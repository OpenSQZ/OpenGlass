# -*- coding: utf-8 -*-
"""cam_tuner.py — ESP32 相机调参台 (形态1: 纯调参, 不接模型)

目的: 一边看 TCP live 画面, 一边热调分辨率/对焦/quality, 实时看每帧的
      拉普拉斯清晰度分 + 亮度 —— 为三级漏斗一级标定阈值。

用法:
  python cam_tuner.py --ip 10.100.7.68
  浏览器开 http://localhost:8080

注意 (硬约束):
  固件 TCP 5000 是单客户端串行。本工具独占 TCP —— 调参时不要同时跑主程序
  (pc_vlm / demo_esp32), 否则抢同一条 TCP 连接会错位。

依赖: aiohttp, opencv-python(cv2), numpy, requests
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import requests
from aiohttp import web


# ============================================================
# TCP 5000 抓帧 (复用已验证协议; 单连接 + 锁, 后台线程持有)
# ============================================================
class TCPImageClient:
    def __init__(self, ip: str, port: int = 5000, timeout: float = 2.0):
        self.ip, self.port, self.timeout = ip, port, timeout
        self._sock = None
        self._lock = threading.Lock()

    def _connect_locked(self) -> bool:
        try:
            self._sock = socket.create_connection((self.ip, self.port), timeout=self.timeout)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock.settimeout(self.timeout)
            return True
        except Exception:
            self._sock = None
            return False

    def _recv_exactly(self, n: int) -> Optional[bytes]:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def capture(self) -> Optional[bytes]:
        with self._lock:
            for attempt in (1, 2):
                if self._sock is None and not self._connect_locked():
                    return None
                try:
                    self._sock.sendall(b"\x01")
                    hdr = self._recv_exactly(20)
                    if not hdr:
                        raise ConnectionError("no header")
                    magic = struct.unpack_from("<I", hdr, 0)[0]
                    length = struct.unpack_from("<I", hdr, 16)[0]
                    if magic != 0x55AA55AA:
                        raise ConnectionError("bad magic")
                    if length == 0:
                        return None
                    return self._recv_exactly(length)
                except Exception:
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
            return None

    def close(self):
        with self._lock:
            try:
                if self._sock:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None


# ============================================================
# 相机 HTTP 控制 (分辨率/对焦/quality) —— 走 /control, 与 esp32_cam_ctl 一致
# ============================================================
FRAMESIZE = {"VGA": 8, "SVGA": 9, "XGA": 10, "HD": 11, "UXGA": 13}


class CamControl:
    def __init__(self, ip: str, timeout: float = 3.0):
        self.base = f"http://{ip}"
        self.timeout = timeout
        self.sess = requests.Session()
        self.sess.trust_env = False

    def control(self, var: str, val) -> bool:
        try:
            r = self.sess.get(f"{self.base}/control",
                              params={"var": var, "val": val}, timeout=self.timeout)
            return r.status_code == 200
        except Exception:
            return False

    def set_resolution(self, name: str) -> bool:
        v = FRAMESIZE.get(name.upper())
        return self.control("framesize", v) if v is not None else False

    def set_quality(self, q: int) -> bool:
        return self.control("quality", max(0, min(63, int(q))))

    def trigger_af(self) -> bool:
        # 单次对焦: 直接写 OV5640 寄存器 0x3022=0x03 (single auto focus)。
        #   固件用标准 esp32-camera web server, 没有实现 /control?var=af (af 非标准变量),
        #   之前先试 control("af") 会"假成功"(固件忽略)而不真对焦 —— 故直接走 /reg。
        #   固件端应设 g_auto_af=false, 关掉每秒定时硬对焦, 对焦完全由此按需触发。
        try:
            r = self.sess.get(f"{self.base}/reg",
                              params={"reg": 0x3022, "mask": 0xff, "val": 0x03},
                              timeout=self.timeout)
            return r.status_code == 200
        except Exception:
            return False


def rotate_jpeg(jpg: bytes, deg: int) -> bytes:
    """后端真旋转 (顺时针 deg∈{0,90,180,270})。返回旋转后的 JPEG bytes。
    真转而非只转显示: 因为最终自动旋转策略判断后会真喂给模型, 数据路径要一致;
    且清晰度分基于转后图算, 可验证'旋转不改变清晰度'(锐利度与朝向正交)。"""
    d = deg % 360
    if d == 0:
        return jpg
    arr = np.frombuffer(jpg, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return jpg
    if d == 90:
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif d == 180:
        img = cv2.rotate(img, cv2.ROTATE_180)
    elif d == 270:
        img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    else:
        return jpg
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return enc.tobytes() if ok else jpg


def _decode_gray(jpg: bytes):
    arr = np.frombuffer(jpg, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)


def frame_motion(jpg_a: bytes, jpg_b: bytes) -> float:
    """两帧灰度平均绝对差 (0-255)。大 = 画面在动 (快速移动/转头)。
    **先高斯模糊再算差**: 抹掉密集文字的高频细节, 只保留宏观位移 ——
    否则密集文字面(成分面)手持轻微移动时每个小字边缘都产生大量像素差, motion 虚高,
    把可读的图误判成"在动"(真机+OCR校准证实成分面可读却被全拒)。
    漏斗二级用: 一批帧相邻 diff 都大 -> 画面不稳 -> 拒绝(念字必错, 让用户停稳)。"""
    a, b = _decode_gray(jpg_a), _decode_gray(jpg_b)
    if a is None or b is None:
        return 0.0
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    a = cv2.GaussianBlur(a, (7, 7), 0)
    b = cv2.GaussianBlur(b, (7, 7), 0)
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def optical_flow_motion(jpg_a: bytes, jpg_b: bytes, downscale: float = 0.35) -> float:
    """两帧稠密光流(Farneback)的平均位移幅度(像素)。物理含义明确、可解释:
    画面整体移动了多少像素。用于【严重晃动一刀切】—— 相机大幅位移时光流幅度大。
    比 frame_motion(逐像素差)更鲁棒: 光流看运动矢量场, 不被密集文字的高频细节干扰
    (密集字轻微移动 -> 光流一致小位移; 逐像素差却虚高)。降采样提速(判"严重"够用)。"""
    a, b = _decode_gray(jpg_a), _decode_gray(jpg_b)
    if a is None or b is None:
        return 0.0
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    if downscale != 1.0:
        a = cv2.resize(a, None, fx=downscale, fy=downscale, interpolation=cv2.INTER_AREA)
        b = cv2.resize(b, None, fx=downscale, fy=downscale, interpolation=cv2.INTER_AREA)
    flow = cv2.calcOpticalFlowFarneback(a, b, None,
                                        pyr_scale=0.5, levels=3, winsize=15,
                                        iterations=3, poly_n=5, poly_sigma=1.2, flags=0)
    mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
    # 位移幅度按降采样比例还原到原图尺度, 便于用原图像素单位设阈值
    return float(mag.mean() / max(downscale, 1e-6))


def bg_signals(jpg_bytes):
    """候选'背景干扰'信号(都可解释, 检测画面有额外背景 -> 提示拿近/对准)。
    干净白纸标签: 大片均匀白 + 中间少量黑字; 有背景(灯管/工位/天花板)时信号异常。
    也含清晰度类(local_sharp/worst_block/sharp_ratio)供评分。返回 dict。"""
    arr = np.frombuffer(jpg_bytes, np.uint8)
    g = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if g is None:
        return {}
    h, w = g.shape
    # 信号1: 亮度直方图熵 —— 干净标签(白底+黑字)分布集中熵低; 背景杂->熵高
    hist = cv2.calcHist([g], [0], None, [32], [0, 256]).flatten()
    p = hist / (hist.sum() + 1e-9)
    entropy = float(-np.sum(p * np.log2(p + 1e-12)))
    # 信号2: 最大均匀(低方差)区占比 —— 白纸有大片均匀白; 占比低=画面杂乱
    blur = cv2.GaussianBlur(g, (5, 5), 0)
    localvar = cv2.blur((g.astype(np.float32) - blur.astype(np.float32)) ** 2, (15, 15))
    lowvar = (localvar < 30).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(lowvar)
    max_uniform = float(stats[1:, cv2.CC_STAT_AREA].max() / g.size) if n > 1 else 0.0
    # 信号3: 边缘空间分布 —— 干净标签边缘集中中间(文字); 背景使边缘散布/落外围
    edges = cv2.Canny(g, 50, 150)
    ys, xs = np.where(edges > 0)
    if len(xs) > 10:
        cx, cy = w / 2, h / 2
        d = np.sqrt(((xs - cx) / w) ** 2 + ((ys - cy) / h) ** 2)
        edge_spread = float(d.mean())
        edge_periph = float((d > 0.35).mean())     # 边缘落外围(远离中心)比例
    else:
        edge_spread = 0.0; edge_periph = 0.0
    # 信号4: local_sharp / worst_block —— 有高对比背景(灯管)时 local虚高而整体糊
    lap = cv2.Laplacian(g, cv2.CV_64F)
    local_sharp = float(lap.var())
    grid = 6
    vals = []
    for i in range(grid):
        for j in range(grid):
            blk = g[i*h//grid:(i+1)*h//grid, j*w//grid:(j+1)*w//grid]
            if blk.size == 0:
                continue
            e = cv2.Canny(blk, 50, 150)
            if (e > 0).mean() < 0.006:
                continue
            vals.append(float(cv2.Laplacian(blk, cv2.CV_64F).var()))
    worst = min(vals) if vals else 0.0
    sharp_ratio = float(local_sharp / (worst + 1e-6)) if worst > 0 else 0.0
    return {
        "bright_entropy": round(entropy, 2),
        "max_uniform": round(max_uniform, 3),
        "edge_spread": round(edge_spread, 3),
        "edge_periph": round(edge_periph, 3),
        "sharp_ratio": round(sharp_ratio, 1),
        "local_sharp": round(local_sharp, 1),
        "worst_block": round(worst, 1),
    }


# ============================================================
# 三级漏斗 (形态2核心)
#   输入: 一批 N 帧 (bytes list)
#   一级 单帧质量筛: 太糊/太暗/空白 的帧标记不合格
#   二级 稳定性判定: 相邻帧 motion 都过大 -> 整批拒绝 (画面不稳)
#   三级 选优: 合格帧里选 sharpness 最高的 = best
#   全不合格 / 不稳 -> 返回拒绝 (安全闸, 不喂模型)
#
#   阈值说明: 分辨率固定 HD 后针对 HD 标定。这里给的是初始默认值,
#   你戴眼镜实测后按 CSV 数据调 (尤其 SHARP_MIN / MOTION_MAX)。
#   sharp 低 + edge 低 = 空白(拒绝, 说"没看到文字"); 用 EDGE_MIN 兜。
# ============================================================
import math


def _g_sat(x: float, x0: float) -> float:
    """Michaelis-Menten 饱和: x/(x+x0) -> [0,1)。x0=半饱和点。"""
    x = max(0.0, x)
    return x / (x + x0) if (x + x0) > 0 else 0.0


def _g_light(b: float, lo: float, hi: float, soft: float) -> float:
    """亮度响应: 太暗/过曝降分, 中间平台=1。两 sigmoid 相夹。"""
    def sig(z):
        try:
            return 1.0 / (1.0 + math.exp(-z))
        except OverflowError:
            return 0.0 if z < 0 else 1.0
    return sig((b - lo) / soft) * (1 - sig((b - hi) / soft))


def frame_score(metrics: dict, rel_change: float, cfg: "FunnelConfig") -> dict:
    """对一帧算 quality + focus_gain + 各分量 (供解释/记录)。
    rel_change: 该帧相对上一帧的 sharp 相对变化 (稳定性输入); 首帧传 0。"""
    # 清晰度用 worst_block(最糊的有内容块) 而非全局 sharpness ——
    #   全局拉普拉斯被大面积白底稀释(白底无边缘, 拉普拉斯低), 把"清晰的白盒子标签"误判成糊,
    #   触发假的 need_focus。worst_block 只看有内容的最糊块, 不被白底拉低。
    #   真机+OCR校准: 不可读帧 worst_block<4.6, 可读>5.5 -> 分界~5, 故 x0 用 worst_x0(~5量级)。
    worst = metrics.get("worst_block", None)
    if worst is None:
        worst = metrics.get("sharpness", 0.0)      # 兜底: 无 worst_block 时退回全局
    g_sharp = _g_sat(worst, cfg.worst_x0)
    g_stable = max(0.0, 1.0 - min(1.0, rel_change))   # rel_change 已归一化到[0,1]
    g_content = _g_sat(metrics.get("edge_density", 0.0), cfg.edge_x0)
    g_light = _g_light(metrics.get("brightness", 0.0), cfg.light_lo, cfg.light_hi, cfg.light_soft)

    wsum = cfg.w_sharp + cfg.w_stable + cfg.w_content + cfg.w_light
    base = (cfg.w_sharp * g_sharp + cfg.w_stable * g_stable
            + cfg.w_content * g_content + cfg.w_light * g_light) / wsum
    sharp_gate = g_sharp ** cfg.sharp_gate_gamma       # 念字硬要求: 糊则压分
    quality = base * sharp_gate
    # focus_gain: 稳 + 糊 时该对焦。对 content 用"温和依赖"(sqrt)而非硬乘 ——
    #   重度失焦会把边缘也糊没(g_content 低), 但它恰恰最该对焦, 不能因此归零;
    #   sqrt 让低内容时 focus_gain 降低但保留触发机会。真空白由 g_content 极低 +
    #   g_stable 高的组合另行识别(见 run_funnel 拒绝归因)。
    focus_gain = g_stable * (1 - g_sharp) * math.sqrt(max(0.0, g_content))
    return {
        "quality": round(quality, 3),
        "focus_gain": round(focus_gain, 3),
        "g_sharp": round(g_sharp, 3),
        "g_stable": round(g_stable, 3),
        "g_content": round(g_content, 3),
        "g_light": round(g_light, 3),
    }


class FunnelConfig:
    """评分函数 f 的参数 (替代硬阈值 if)。每帧算一个连续 quality 分 + focus_gain,
    决策基于分数而非离散阈值 —— 可解释、可 ablation、可用数据校准。

    quality = [Σ wi·gi] · g_sharp^gamma      (sharp 作乘性门, 念字硬要求)
      g_sharp   = sharp/(sharp+sharp_x0)      清晰度饱和响应
      g_stable  = 1 - min(1, rel_change/change_ref)   帧间相对变化越小越稳
      g_content = edge/(edge+edge_x0)         有无内容(区分糊/空)
      g_light   = 亮度双sigmoid(暗/过曝都降)
    focus_gain = g_stable·(1-g_sharp)·g_content   稳+糊+有内容 -> 该对焦
    决策: max(quality) >= ACCEPT_Q -> 选该帧; 否则看 max(focus_gain) >= FOCUS_G
          -> 触发对焦再采; 都低 -> 拒绝。
    参数是物理动机初值, 戴眼镜采数据后可校准/拟合 (方法 vs 调参的区别)。
    """
    # g 响应形状 (半饱和点/参考值)
    sharp_x0 = 120.0      # (记录用) 全局清晰度半饱和点; g_sharp 现改用 worst_block
    worst_x0 = 6.5        # worst_block 半饱和点。调严(原5.0)要求更清晰。真机+OCR: 不可读<4.6/可读>5.5。
                          #   g_sharp = worst/(worst+worst_x0): worst=5时g_sharp=0.5, 白盒清晰标签
                          #   worst 正常(不被白底稀释)故不误判need_focus。阈值待白标签标准数据精校。
    change_ref = 0.5      # (旧, 已弃用: 稳定性改用帧间像素差)
    motion_ref = 15.0     # 帧间模糊后平均像素差达此视为"完全在动"。校准依据: OCR证实可读的
                          #   成分面(密集字)模糊后 motion 达~8.4 仍可读, 须能过; 故 ref 上调到15,
                          #   使可读帧 stable>=0.44 通过, 同时剧烈晃动(>15)仍被拒。下界待补采晃动样本精校。
    edge_x0 = 0.01        # 边缘密度半饱和 (区分有内容/空白)
    light_lo = 40.0       # 亮度下沿
    light_hi = 220.0      # 亮度上沿(过曝)
    light_soft = 25.0     # 亮度软边宽度
    # 权重 (语义清晰, 可做 ablation)
    w_sharp = 1.0
    w_stable = 1.0
    w_content = 0.6
    w_light = 0.4
    sharp_gate_gamma = 0.5  # sharp 乘性门强度 (0=不否决, 大=糊帧强烈压分)
    # 决策阈值 (作用在归一化综合分上, 只此两个, 远少于原来一堆硬阈值)
    ACCEPT_Q = 0.55       # 可用性门: 调严(原0.40太松, 放过模糊图)。0.55 要求更清晰才放行。
                          #   宁拒绝不念错: 送下游的必须够清晰, 模糊的宁可让用户重拍。
                          #   放宽的理由(数据得出): 全局quality分不开'能OCR'与'轻微糊',
                          #   那是任务层的精判(局部/文字区清晰度), 通用层不越俎代庖。
    FOCUS_G = 0.35        # (保留) focus_gain 参考
    # 稳定性优先三出口的门限 (段级中位数上判定; 待真机 rerun 校准)
    STABLE_MIN = 0.35     # 出口A: g_stable 低于此 = 持续在动 -> "请拿稳" (第一闸)。
                          #   从0.45降到0.35: 配合 motion_ref=15, 让OCR证实可读的密集字面通过, 修复误拒。
    SEVERE_FLOW = 8.0     # 出口A0: 段级光流位移(像素)>=此 = 严重晃动一刀切拒绝。初值8,
                          #   待 shake 样本校准("多大位移算严重"); 光流位移物理可解释。
    LIGHT_MIN = 0.35      # 出口D(留): g_light 低于此且为最差 -> 太暗
    CONTENT_MIN = 0.30    # 出口E(留): g_content 低于此
    SHARP_OK_FOR_E = 0.45 # 出口E(留): 判"没对准"要求 sharp 够高(清晰却没字才算对错)
    MIN_QUALIFIED = 1


# ============================================================
# 场景配置 (核心: 机制一套, 参数按场景标定 —— "权重由场景物理特征+安全等级决定")
#   药盒(medicine): 小字/高危/白底 -> 偏拒绝、要求清晰、零容忍。= 默认 FunnelConfig。
#   文具(stationery): 中大字/低危/彩色包装 -> 可放松、允许更多放行。
#   固有机制(对焦/多帧筛选/倒置/光流晃动)完全一致, 只有下面这些判定参数不同。
# ============================================================
def make_scene_config(scene: str = "medicine") -> "FunnelConfig":
    """按场景返回标定好的 FunnelConfig。机制代码不变, 只改参数。"""
    cfg = FunnelConfig()
    if scene in ("medicine", "药盒", "drug"):
        return cfg                                # 药盒 = 现有默认(小字/高危/严)
    if scene in ("stationery", "文具", "supplies", "生活用品"):
        # 生活用品/文具: 安全等级低(念错关系不大) -> 放行阈值放松, 少拒绝、少打扰。
        # 参数依据: 网络摄像头 clip_windows csv (routing 分析) + 安全等级。
        cfg.worst_x0 = 5.0       # 松(药盒6.5): 拦截判据, 药盒严文具松(安全等级低)。
                                 #   手动定: csv里 worst_block 对ok/blur区分很弱(图都较清晰,
                                 #   AUC~0.52), 算不出可靠值, 按"文具比药盒松"手动定松一档。
        cfg.ACCEPT_Q = 0.48      # 略放松(药盒0.55): 安全低, 容错高一点。
        cfg.SEVERE_FLOW = 8.0    # 沿用药盒: csv 里 seg_flow 是最强判据(AUC~0.71), >8 后
                                 #   ok率骤降, 与药盒一致。晃动是通用物理约束, 不随场景放松。
        return cfg
    # 未知场景 -> 退回药盒默认, 并提示
    print(f"[scene] 未知场景 '{scene}', 用药盒默认配置")
    return cfg


class FunnelResult:
    def __init__(self):
        self.accepted = False
        self.need_focus = False       # 稳但糊: 上层应触发AF再采一批
        self.reject_reason = ""
        self.best_index = -1
        self.best_jpg = None
        self.per_frame = []
        self.motions = []             # 这里存 rel_change 序列
        self.components = {}          # 段级各 gi 分量 (归因用)
        self.timings = {}

    def to_dict(self):
        def _fmt(v):
            if isinstance(v, (int, float)):
                return round(v, 1)
            return v   # list 之类原样 (如 grab_per_frame_ms)
        return {
            "accepted": self.accepted,
            "need_focus": self.need_focus,
            "reject_reason": self.reject_reason,
            "best_index": self.best_index,
            "per_frame": self.per_frame,
            "rel_changes": [round(m, 3) for m in self.motions],
            "components": self.components,
            "timings": {k: _fmt(v) for k, v in self.timings.items()},
        }


def run_funnel(frames: list[bytes], cfg: FunnelConfig = FunnelConfig()) -> FunnelResult:
    """评分函数版漏斗:
      1) 每帧算 metrics(sharp/亮度/edge)
      2) 帧间相对变化 -> 每帧 quality + focus_gain (连续分, 非硬阈值)
      3) 决策: max(quality)>=ACCEPT_Q -> 选该帧(选优);
               否则 max(focus_gain)>=FOCUS_G -> 建议对焦(need_focus);
               都低 -> 拒绝。
    返回里带 need_focus 标志, 供上层决定"稳但糊->触发AF->再采"。"""
    r = FunnelResult()
    if not frames:
        r.reject_reason = "no_frames"
        return r

    # 1) 单帧 metrics
    t0 = time.monotonic()
    metrics_list = [frame_metrics(j) for j in frames]
    r.timings["level1_metrics_ms"] = (time.monotonic() - t0) * 1000

    # 2) 帧间"运动"(稳定性输入) + 每帧评分
    #   稳定性用真正的帧间像素差分(frame_motion)衡量 —— 位移/转动会改变画面内容,
    #   但不一定改变清晰度, 故不能用 sharp 变化代替。归一化到 [0,1] 的 rel。
    t1 = time.monotonic()
    per = []
    prev_jpg = None
    flow_mags = []                                    # 相邻帧光流位移幅度(严重晃动一刀切用)
    for i, m in enumerate(metrics_list):
        if prev_jpg is None:
            motion = 0.0
        else:
            motion = frame_motion(prev_jpg, frames[i])   # 平均绝对像素差 0-255
            flow_mags.append(optical_flow_motion(prev_jpg, frames[i]))  # 光流位移(像素)
        prev_jpg = frames[i]
        rel = min(1.0, motion / cfg.motion_ref)          # 归一: motion>=motion_ref 视为完全在动
        sc = frame_score(m, rel, cfg)
        per.append({**m, "rel_change": round(rel, 3), "motion": round(motion, 2), **sc})
        r.motions.append(rel)
    r.per_frame = per
    # 段级光流: 用中位数抗单帧噪声。大 = 整段相机在大幅移动 = 严重晃动。
    seg_flow = float(sorted(flow_mags)[len(flow_mags) // 2]) if flow_mags else 0.0
    r.timings["level2_score_ms"] = (time.monotonic() - t1) * 1000

    # 3) 决策: 稳定性优先的三出口 (A/B/C), argmin(gi) 归因, D/E 留接口
    t2 = time.monotonic()
    qualities = [p["quality"] for p in per]
    # 段级聚合各分量 (用中位数抗单帧噪声)
    def med(key):
        vals = sorted(p[key] for p in per)
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    seg_stable = med("g_stable")
    seg_sharp = med("g_sharp")
    seg_content = med("g_content")
    seg_light = med("g_light")
    # best 帧选择: 只按【清晰度】选最能读的一帧, 不用综合 quality。
    #   原因: quality 含 stable 分量, 会让"晃动某瞬间恰好帧间motion低(转折点)但画面糊"的帧
    #   quality 虚高, 压过"稍动但清晰可读"的帧 -> best 选成糊的(真机验证: f0糊被选/f2清晰没选)。
    #   念字能不能读只取决于清晰度, 与该帧瞬时稳不稳无关; 稳定性只用于整段是否拒绝(出口A)。
    #   用 worst_block(最糊有内容块)选: 选"最糊处也最清晰"的那帧 = 最可读。
    def _clarity(i):
        return per[i].get("worst_block", per[i].get("sharpness", 0.0))
    best_q = max(range(len(per)), key=_clarity)
    # quality 仍保留供出口B的接受阈值判断(下面 qualities[best_q]>=ACCEPT_Q)
    r.timings["level3_decide_ms"] = (time.monotonic() - t2) * 1000

    # 归因分量表 (供 argmin 路由 + 记录)
    r.components = {"stable": round(seg_stable, 3), "sharp": round(seg_sharp, 3),
                    "content": round(seg_content, 3), "light": round(seg_light, 3),
                    "flow": round(seg_flow, 2)}      # 段级光流位移(像素)

    # ---- 出口 A0: 严重晃动一刀切 —— 光流位移过大 = 相机大幅移动, 不看别的直接拒 ----
    #   可解释: 画面整段平均移动 >SEVERE_FLOW 像素 = 明显在晃, 念字必糊, 让用户停稳。
    #   光流比逐像素差鲁棒(不被密集字伪运动骗); 阈值待 shake 样本校准。
    if seg_flow >= cfg.SEVERE_FLOW:
        r.reject_reason = "severe_shake"          # 出口 A0: "晃得厉害, 请拿稳"
        r.need_focus = False
        r.best_index = best_q
        return r

    # ---- 出口 A: 稳定性优先 —— 持续在动, 不看清晰度, 直接请拿稳 ----
    #   (动的时候对焦也没用, 且用户没老实, 不该浪费时间去挑图)
    if seg_stable < cfg.STABLE_MIN:
        r.reject_reason = "unstable"          # 出口 A: "请拿稳/对准"
        r.need_focus = False
        r.best_index = best_q                 # 仅供参考, 不喂模型
        return r

    # ---- 稳定。看有没有帧够清晰 ----
    if qualities[best_q] >= cfg.ACCEPT_Q:
        # ---- 出口 B: 稳 + 有清晰帧 -> 给最清晰的 ----
        r.accepted = True
        r.best_index = best_q
        r.best_jpg = frames[best_q]
        r.need_focus = False
        return r

    # ---- 稳但没有够清晰的帧: 归因 argmin, 决定是 C(对焦) 还是 D/E ----
    #   在"稳定"前提下, 看哪个分量最拖累: sharp低=失焦(C); light低=暗(D);
    #   content低但sharp高=清晰却没内容=没对准(E)。argmin 连续路由, 非硬堆阈值。
    cand = {"sharp": seg_sharp, "light": seg_light, "content": seg_content}
    worst = min(cand, key=cand.get)

    if worst == "light" and seg_light < cfg.LIGHT_MIN:
        # 出口 D (留接口, 未激活干预): 太暗 -> 未来 CLAHE 数字增强 / 提示到亮处
        r.reject_reason = "too_dark"          # TODO: 接 CLAHE 后改为可救
        r.need_focus = False
        r.best_index = best_q
        return r

    # 出口 E（aimed_wrong）已弃用：原设计（-v/OCR）用"清晰但无内容结构"判"没对准"，
    #   目的是排除背景干扰。但 -o 不受背景影响、完全没字时会老实说"没看到字"，
    #   不需要漏斗拦。故这种帧直接放行，交给模型自己判断，避免误判打断。
    #   （真正危险的"像字又不是字"是另一个问题，待 720p 数据用别的信号重做。）
    if worst == "content" and seg_sharp >= cfg.SHARP_OK_FOR_E and seg_content < cfg.CONTENT_MIN:
        r.accepted = True
        r.best_index = best_q
        r.best_jpg = frames[best_q]
        r.need_focus = False
        return r

    # ---- 出口 C: 稳但糊 (sharp 主导拖累) -> 触发AF重采 ----
    r.accepted = False
    r.need_focus = True
    r.reject_reason = "need_focus"            # 上层触发AF+重采, 仍不行才最终拒绝
    r.best_index = best_q
    return r


# ============================================================
# 方向处理 & 推理 —— 留桩, 下一步填
# ============================================================
def _center_crop(gray, frac=0.6):
    """取画面中心区, 排掉四周背景(手/桌面/墙)。用户会把目标大致对准中心,
    故中心区更可能是文字主体; 在其上算判据可显著减轻背景污染。"""
    h, w = gray.shape[:2]
    y0, y1 = int(h * (1 - frac) / 2), int(h * (1 + frac) / 2)
    x0, x1 = int(w * (1 - frac) / 2), int(w * (1 + frac) / 2)
    return gray[y0:y1, x0:x1]


def _binv_fg(gray):
    _, b = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return (b > 0).astype(np.float64)


def _detect_sideways(gray):
    """90°/270°侧向: 水平投影方差 vs 垂直投影方差。多档中心区(0.5/0.6/0.7)投票,
    比单一比例更稳(文字不在正中心时不易被单档背景污染带偏)。
    返回 (verdict, ratio): 'upright'/'sideways'/'ambiguous'。ratio 取中位数。"""
    ratios = []
    for frac in (0.5, 0.6, 0.7):
        b = _binv_fg(_center_crop(gray, frac))
        hvar = float(np.var(b.sum(axis=1)))
        vvar = float(np.var(b.sum(axis=0)))
        ratios.append(hvar / (vvar + 1e-6))
    ratios.sort()
    med = ratios[1]                        # 中位数, 抗单档异常
    votes = ["sideways" if r < 0.67 else ("upright" if r > 1.5 else "ambiguous")
             for r in ratios]
    # 多数票: 3档里≥2档一致才定, 否则 ambiguous
    for v in ("sideways", "upright"):
        if votes.count(v) >= 2:
            return v, round(med, 2)
    return "ambiguous", round(med, 2)


def _detect_upside_down(gray):
    """180°正倒: '注水容量'判据(资料方法, 真图验证对中文有效)。
    对每列: 最高文字点上方空白(top_cap) vs 最低文字点下方空白(bot_cap)。
    正立版面文字整体下方留白多 -> (bot-top)>0; 倒置翻转 -> <0。
    **看版面级上下留白, 不依赖单字对称性, 绕开中文字对称死结。**
    真图验证: 正立+0.66/倒置-0.48, 抗±8°倾斜、抗裁剪比例(0.5~0.9)。必须裁剪(整图会被背景淹)。
    返回 (verdict, score): 'upright'/'flipped'/'ambiguous'。"""
    b = _binv_fg(_center_crop(gray, 0.7))
    h, w = b.shape
    tc = bc = cnt = 0
    for x in range(0, w, 3):
        col = np.where(b[:, x] > 0)[0]
        if len(col) < 2:
            continue
        tc += col[0]
        bc += (h - 1 - col[-1])
        cnt += 1
    if cnt < 10:
        return "ambiguous", 0.0
    score = (bc - tc) / (bc + tc + 1e-9)
    if score > 0.15:
        return "upright", round(score, 3)
    if score < -0.15:
        return "flipped", round(score, 3)
    return "ambiguous", round(score, 3)


def _detect_truncation(gray, margin=10):
    # 先确认中心区有文字主体; 否则不判截断(避免背景边缘误报)
    cb = _binv_fg(_center_crop(gray, 0.6))
    if cb.mean() < 0.01:
        return {}, {}                      # 中心没内容, 不谈截断
    b = _binv_fg(gray)
    edges = {"top": b[:margin, :].mean(), "bottom": b[-margin:, :].mean(),
             "left": b[:, :margin].mean(), "right": b[:, -margin:].mean()}
    overall = b.mean()
    if overall < 1e-4:
        return {}, {}
    # 贴边判据收紧: 边缘密度需 >= 整图的一定比例, 且 >= 中心密度的一定比例
    #   (排掉"背景纹理贴边但中心才是文字"的误报)
    center_d = cb.mean()
    touched = {k: (v >= overall * 0.6 and v >= center_d * 0.4 and v > 0.02)
               for k, v in edges.items()}
    return {k: round(v, 4) for k, v in edges.items()}, {k: t for k, t in touched.items() if t}


_PAN_HINT = {"top": "请向上看一点", "bottom": "请向下看一点",
             "left": "请向左看一点", "right": "请向右看一点"}


_ORI_CLS = None            # 方向分类器单例(全局只加载一次)
_ORI_CLS_TRIED = False     # 是否已尝试加载(避免反复重试失败)

def _get_orient_classifier():
    """懒加载 PaddleOCR 文档方向分类器(PP-LCNet, 6.75MB, 0/90/180/270)。
    单例: 只在首次调用时加载一次(几秒), 之后每次推理仅几ms。
    降级: 未安装/加载失败 -> 返回 None, process_orientation 回退到轻量CV判据, 不崩。"""
    global _ORI_CLS, _ORI_CLS_TRIED
    if _ORI_CLS is not None:
        return _ORI_CLS
    if _ORI_CLS_TRIED:
        return None                        # 之前试过且失败, 不再重试
    _ORI_CLS_TRIED = True
    # 加载方向分类器。优先用最简单的写法(实测 SmartGlasses 环境可用, 与 torch 不冲突 ——
    #   当初冲突的是完整 PaddleOCR-GPU 识别, 不是这个轻量方向分类器)。
    #   若想显式指定设备, 后面的参数变体作为备选依次尝试。
    from paddleocr import DocImgOrientationClassification
    last_err = None
    for kw in (dict(model_name="PP-LCNet_x1_0_doc_ori"),                    # 最简单(原来能用的)
               dict(model_name="PP-LCNet_x1_0_doc_ori", device="cpu"),     # 显式CPU(可选)
               dict()):                                                     # 全默认兜底
        try:
            _ORI_CLS = DocImgOrientationClassification(**kw)
            print(f"[orient] PaddleOCR 方向分类器已加载 (PP-LCNet, 参数={list(kw) or 'default'})")
            return _ORI_CLS
        except Exception as e:
            last_err = e
            continue                           # 这个参数组合不行, 换下一个
    print(f"[orient] 方向分类器加载失败, 回退到轻量CV判据: {last_err}")
    _ORI_CLS = None
    return None


# 文档方向标签(图像被顺时针旋转的角度) -> (状态, 给用户的转向提示)
#   注意: "图像旋转角"与"用户转药盒方向"相反。文案先按标准定义, 实机核对后固定。
#   90°: 画面顺时针转了90° = 药盒被逆时针放了 -> 用户应顺时针转回? 需实机核对, 故保留 raw 标签调试。
_ORIENT_MAP = {
    "0":   ("upright", None),
    "90":  ("sideways", "请把药盒顺时针转90°"),
    "270": ("sideways", "请把药盒逆时针转90°"),
    "180": ("flipped", "药盒拿反了，请上下翻转"),
}


def _classify_orientation(img_bgr):
    """用分类器判方向。返回 (raw_label, conf) 或 (None, 0) 若不可用/低置信。"""
    cls = _get_orient_classifier()
    if cls is None:
        return None, 0.0
    try:
        res = cls.predict(img_bgr)          # 传 ndarray, 免落盘
        r = res[0]
        labels = r.get("label_names") or [str(x) for x in r.get("class_ids", [])]
        scores = r.get("scores", [0.0])
        raw = str(labels[0]).replace("_degree", "").strip()
        conf = float(max(scores)) if scores else 0.0
        return raw, conf
    except Exception as e:
        print(f"[orient] 推理失败: {e}")
        return None, 0.0


def process_orientation(best_jpg: bytes):
    """第2级(方向) + 第3级(取景完整) —— 只对清晰的 best 帧做一次。
    方向: 优先用 PaddleOCR 轻量方向分类器(PP-LCNet, 真图验证0/90/180/270全准, 置信~0.92);
          未装/低置信 -> 回退轻量CV判据。180°走多帧一致性('请确认正反'在handler触发)。
    返回 (jpg, diag)。diag 含方向反馈 + 截断反馈 + incomplete 标志(传下游prompt)。
    [留桩] 圆柱曲面矫正 = 独立课题, 不在此处。"""
    arr = np.frombuffer(best_jpg, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return best_jpg, {"ok": False}
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    orient_hint = None
    orient_state = "upright"
    ud_score = 0.0
    side_ratio = ""
    orient_raw = ""
    orient_conf = 0.0
    CONF_MIN = 0.60                          # 置信门控: 低于此不硬判, 走uncertain/兜底

    raw, conf = _classify_orientation(img)
    orient_raw, orient_conf = (raw or ""), conf
    if raw is not None and conf >= CONF_MIN and raw in _ORIENT_MAP:
        # 分类器可用且置信足够 -> 直接按映射给状态+提示
        # (实测180 conf~0.93很确定, 不会与0混淆, 故即时提示; 若需滤噪声在簇内多帧投票层做)
        orient_state, orient_hint = _ORIENT_MAP[raw]
    elif raw is not None:
        # 分类器给了结果但低置信 -> 不硬判
        orient_state = "uncertain"
    else:
        # 分类器不可用 -> 回退轻量CV判据(整块投影/注水容量)
        side, side_ratio = _detect_sideways(gray)
        if side == "sideways":
            orient_state, orient_hint = "sideways", "请把药盒转90°"
        else:
            ud, ud_score = _detect_upside_down(gray)
            orient_state = {"flipped": "flipped", "upright": "upright"}.get(ud, "uncertain")

    # 取景完整性(截断) —— 显示层暂关, 接口保留
    edges, truncated = _detect_truncation(gray)
    pan_hint = None
    if truncated:
        worst = max(truncated, key=lambda k: edges.get(k, 0))
        pan_hint = _PAN_HINT.get(worst)

    diag = {
        "ok": True,
        "orient_state": orient_state,        # upright/sideways/flipped/uncertain
        "orient_hint": orient_hint,          # 侧向即时提示; 180的"请确认"由多帧一致性在handler加
        "orient_raw": orient_raw,            # 分类器原始标签(0/90/180/270), 实机核对转向用
        "orient_conf": round(orient_conf, 3),
        "sideways_ratio": side_ratio,
        "flipped_frame": orient_state == "flipped",   # 供多帧一致性累积
        "ud_score": ud_score,
        "truncated_edges": list(truncated.keys()),
        "pan_hint": pan_hint,
        "incomplete": bool(truncated),       # 传下游prompt: 只念可见部分
    }
    return best_jpg, diag   # 第一版不自动转正, 只诊断+提示(方向由用户调)


def infer(frame_jpg: bytes) -> Optional[str]:
    """[留桩] 未来: 把 best 帧喂给 -v (或 Qwen-VL) 做念字/识别。
    现在返回 None。接推理时把这里换成真的模型调用。"""
    return None


# ============================================================
# CV 指标: 拉普拉斯方差(清晰度) + 平均亮度
#   这两个就是三级漏斗一级的判据; 在这里实时显示以标定阈值。
# ============================================================
def frame_metrics(jpg: bytes) -> dict:
    """返回一帧的 CV 指标字典。记录器会动态把所有 key 当作 CSV 列 ——
    以后 CV 那步加新指标(如帧间差分测晃动、方向检测分), 只需在这里往
    dict 里加 key, CSV 自动多列, 记录代码不用改。"""
    arr = np.frombuffer(jpg, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return {"sharpness": 0.0, "brightness": 0.0, "kb": round(len(jpg) / 1024, 1)}
    lap_var = cv2.Laplacian(img, cv2.CV_64F).var()
    edges = cv2.Canny(img, 80, 160)
    edge_density = float((edges > 0).mean())

    # --- 附加指标: 先记录不判定, 采数据后用 ok/blur 标注看哪个最能对齐可读性 ---
    # local_sharp: N×N 分块取最清晰块。抓"局部有清晰区"; 但会被高频背景(木纹)骗高。
    ls = _local_sharp(img, grid=4)
    # text_sharp / text_ratio: MSER 类文字候选区的清晰度 + 面积占比。
    #   text_ratio 说"有没有像字的区域"(高频背景 ratio≈0, 骗不过它);
    #   但糊到一定程度 MSER 找不到字 -> ratio→0 (自我矛盾, 已离线证实)。
    #   与 local_sharp 组合: local高+ratio低=清晰但非字; 两者皆低=糊。
    ts, tr = _text_sharp(img)
    # worst_block / block_var: 匹配"一处糊即不可用(OCR 标准)"的悲观判据。
    #   worst_block = 有内容的块里最糊的清晰度 (看最差, 非最好);
    #   block_std   = 块间清晰度标准差 (大 = 部分清晰部分糊 = 局部糊)。
    #   对 OCR: 决定成败的是最糊的文字区, 不是最清晰的 —— 故看 worst 而非 best。
    wb, bstd = _block_sharp_stats(img, grid=4)
    # contrast / dark_ratio: 区分"糊"与"暗/低对比"。暗+低对比时全局sharp假性偏低,
    #   但 contrast 低会揭示真因 -> 对应"数字增强(CLAHE)"而非"对焦"这条干预路径。
    contrast = float(img.std())
    dark_ratio = float((img < 50).mean())

    # [CLAHE 接口预留] 未来暗环境念字: 若 contrast 低 / dark_ratio 高, 可在此对 img
    #   做 CLAHE/gamma 增强后重算指标, 作为"数字层干预"(区别于对焦的"物理层干预")。
    #   现在不实现 —— 待有暗环境测试条件再填。示意:
    #   if contrast < TH: img_enh = cv2.createCLAHE(...).apply(img); 重算 lap_var ...

    return {
        "sharpness": round(float(lap_var), 1),      # 全局拉普拉斯方差
        "brightness": round(float(img.mean()), 1),
        "edge_density": round(edge_density, 4),
        "local_sharp": round(float(ls), 1),          # 最清晰块 (乐观, 记录)
        "worst_block": round(float(wb), 1),           # 最糊的有内容块 (悲观, 匹配OCR标准)
        "block_std": round(float(bstd), 1),           # 块间清晰度std (大=局部糊)
        "text_sharp": round(float(ts), 1),           # 文字区清晰度 (记录)
        "text_ratio": round(float(tr), 4),           # 文字区面积占比 (记录)
        "contrast": round(contrast, 1),              # 对比度(std) (记录)
        "dark_ratio": round(dark_ratio, 4),          # 暗像素占比 (记录)
        "kb": round(len(jpg) / 1024, 1),
    }


def _block_sharp_stats(gray, grid: int = 4):
    """分块清晰度的 (最糊有内容块, 块间std)。
    只统计"有内容"的块(edge 非极低), 避免纯背景块干扰;
    worst = 有内容块里最低清晰度 (对应'一处糊即失败');
    std   = 块间清晰度离散度 (局部糊 -> 大)。"""
    h, w = gray.shape[:2]
    vals = []
    for i in range(grid):
        for j in range(grid):
            blk = gray[i * h // grid:(i + 1) * h // grid, j * w // grid:(j + 1) * w // grid]
            if blk.size == 0:
                continue
            # 只算"有文字/边缘结构"的块。关键: 用【边缘密度】而非 std 判有内容 ——
            #   std 只反映明暗起伏, 白纸的折痕/阴影/渐变 std 也 >8, 会被误当"内容块",
            #   而白纸拉普拉斯极低(~1) -> 把 worst_block 拉到假性极低 -> 清晰标签被误判糊。
            #   文字块有大量边缘, 白纸光影块几乎无边缘 -> 用 Canny 边缘占比区分。
            edges = cv2.Canny(blk, 50, 150)
            edge_frac = float((edges > 0).mean())
            if edge_frac < 0.006:          # 边缘太少 = 无文字结构(白纸/背景) -> 跳过
                continue
            vals.append(float(cv2.Laplacian(blk, cv2.CV_64F).var()))
    if not vals:
        return 0.0, 0.0
    worst = min(vals)
    std = float(np.std(vals)) if len(vals) > 1 else 0.0
    return worst, std


def _local_sharp(gray, grid: int = 4) -> float:
    """N×N 分块, 取最清晰块的拉普拉斯方差。"""
    h, w = gray.shape[:2]
    best = 0.0
    for i in range(grid):
        for j in range(grid):
            blk = gray[i * h // grid:(i + 1) * h // grid, j * w // grid:(j + 1) * w // grid]
            if blk.size:
                best = max(best, float(cv2.Laplacian(blk, cv2.CV_64F).var()))
    return best


def _text_sharp(gray):
    """MSER 找类文字候选区, 在候选区上算清晰度。返回 (清晰度, 面积占比)。
    找不到 -> (0,0)。占比=0 提示'没找到字'(可能糊/无字), 与 local_sharp 组合判读。"""
    try:
        mser = cv2.MSER_create(delta=5, min_area=60, max_area=14400)
        regions, _ = mser.detectRegions(gray)
    except Exception:
        return 0.0, 0.0
    if not regions:
        return 0.0, 0.0
    mask = np.zeros(gray.shape[:2], np.uint8)
    for pts in regions:
        x, y, bw, bh = cv2.boundingRect(pts.reshape(-1, 1, 2))
        ar = bw / max(bh, 1)
        if 0.1 < ar < 10 and bw < gray.shape[1] * 0.9:
            cv2.fillPoly(mask, [cv2.convexHull(pts.reshape(-1, 1, 2))], 255)
    area_ratio = float((mask > 0).mean())
    if area_ratio < 0.001:
        return 0.0, area_ratio
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    return float(lap[mask > 0].var()), area_ratio


# ============================================================
# 后台抓帧线程: 独占 TCP, 持续抓最新帧 (供 /frame 取用)
#   前端按自己的刷新率来取, 后端只维护"最新一帧+指标"。
# ============================================================
class Recorder:
    """记录一段一段的指标序列。每段带一个 label(触发来源, 如 af / res:UXGA / manual),
    每帧一行, 动态列(跟随 frame_metrics 的 key)。存磁盘 + 供前端下载。

    两种触发: (a) 点控制按钮自动录 duration 秒; (b) 手动开始/停止。
    记录挂在后端抓帧线程上(每抓一帧记一行), 与前端显示刷新率解耦, 密度=真实抓帧率。"""

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._rows: list[dict] = []      # 所有已记录的行 (跨段, 全量)
        self._active = False
        self._seg_label = ""
        self._seg_start_mono = 0.0
        self._seg_deadline = 0.0         # >0 表示定时录; 0 表示手动录(无限直到 stop)
        self._seg_index = 0
        self._columns: list[str] = []    # 动态列, 首次记录时按 metrics key 确定

    def start_segment(self, label: str, duration_s: float = 0.0):
        """duration_s>0: 定时录; =0: 手动录(直到 stop_segment)。"""
        with self._lock:
            self._active = True
            self._seg_index += 1
            self._seg_label = label
            self._seg_start_mono = time.monotonic()
            self._seg_deadline = (self._seg_start_mono + duration_s) if duration_s > 0 else 0.0

    def stop_segment(self):
        with self._lock:
            self._active = False

    def feed(self, metrics: dict, grab_ms: float, res: str, rotate: int):
        """抓帧线程每帧调一次。仅在 active 且未超时时记录。"""
        with self._lock:
            if not self._active:
                return
            now = time.monotonic()
            if self._seg_deadline > 0 and now >= self._seg_deadline:
                self._active = False
                return
            if not self._columns:
                # 首次: 固定前缀列 + metrics 的动态列
                self._columns = (["seg", "label", "t_ms", "grab_ms", "res", "rotate"]
                                 + list(metrics.keys()))
            row = {
                "seg": self._seg_index,
                "label": self._seg_label,
                "t_ms": round((now - self._seg_start_mono) * 1000, 1),
                "grab_ms": round(grab_ms, 1),
                "res": res,
                "rotate": rotate,
            }
            row.update(metrics)
            self._rows.append(row)

    def status(self) -> dict:
        with self._lock:
            remain = 0.0
            if self._active and self._seg_deadline > 0:
                remain = max(0.0, self._seg_deadline - time.monotonic())
            return {
                "active": self._active,
                "label": self._seg_label,
                "remain_s": round(remain, 1),
                "n_rows": len(self._rows),
                "n_segments": self._seg_index,
            }

    def to_csv(self) -> str:
        import csv, io
        with self._lock:
            rows = list(self._rows)
            cols = list(self._columns) if self._columns else ["seg"]
        # 动态列可能因未来指标增删而不齐: 用所有行 key 的并集补齐
        allcols = list(cols)
        for r in rows:
            for k in r:
                if k not in allcols:
                    allcols.append(k)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=allcols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return buf.getvalue()

    def save_disk(self) -> Optional[Path]:
        csv_text = self.to_csv()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = self.out_dir / f"tuning_{stamp}.csv"
        try:
            path.write_text(csv_text, encoding="utf-8-sig")  # BOM 便于 Excel 直接开
            return path
        except Exception as e:
            print(f"[REC] 存盘失败: {e}")
            return None

    def clear(self):
        with self._lock:
            self._rows.clear()
            self._seg_index = 0
            self._columns = []
            self._active = False


class FrameGrabber:
    def __init__(self, tcp: TCPImageClient, recorder: "Recorder", ctl_state: dict):
        self.tcp = tcp
        self.recorder = recorder
        self.ctl_state = ctl_state       # {"res":..., "rotate":...} 供记录标注当前状态
        self._latest_jpg: Optional[bytes] = None
        self._latest_metrics: dict = {}
        self._latest_grab_ms: float = 0.0
        self._rotate_deg = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def set_rotation(self, deg: int):
        with self._lock:
            self._rotate_deg = deg % 360

    def start(self):
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            jpg = self.tcp.capture()
            dt = (time.monotonic() - t0) * 1000
            if jpg:
                with self._lock:
                    deg = self._rotate_deg
                if deg:
                    jpg = rotate_jpeg(jpg, deg)   # 真转, 指标基于转后图
                mtr = frame_metrics(jpg)
                with self._lock:
                    self._latest_jpg = jpg
                    self._latest_metrics = mtr
                    self._latest_grab_ms = dt
                # 喂记录器 (仅在 active 时真记); 带当前分辨率/旋转状态
                self.recorder.feed(mtr, dt,
                                   res=self.ctl_state.get("res", "?"),
                                   rotate=deg)
            else:
                time.sleep(0.05)

    def latest(self):
        with self._lock:
            return self._latest_jpg, dict(self._latest_metrics), self._latest_grab_ms

    def grab_batch(self, n: int, rotate_apply: bool = True):
        """连抓 n 帧 (复用 TCP 连接, 串行)。返回 (frames_list, grab_ms_list)。
        对应真实链路 ASR 后'抓 N 张'那一步。"""
        frames, lats = [], []
        with self._lock:
            deg = self._rotate_deg
        for _ in range(n):
            t0 = time.monotonic()
            jpg = self.tcp.capture()
            dt = (time.monotonic() - t0) * 1000
            if jpg:
                if rotate_apply and deg:
                    jpg = rotate_jpeg(jpg, deg)
                frames.append(jpg)
                lats.append(dt)
        return frames, lats

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        self.tcp.close()


# ============================================================
# Web 服务
# ============================================================
PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>相机调参台</title>
<style>
  :root {
    --bg: #0f1115; --panel: #161a21; --line: #262c38;
    --ink: #e6e9ef; --dim: #7d8797; --hot: #ff5c4d;
    --good: #4fd18b; --warn: #ffb454; --mono: "SF Mono","JetBrains Mono",Consolas,monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: var(--mono); display: grid;
    grid-template-columns: 1fr 340px; min-height: 100vh;
  }
  /* 左: 画面 */
  .stage { padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  .feed {
    position: relative; background: #000; border: 1px solid var(--line);
    border-radius: 4px; overflow: hidden; aspect-ratio: 4/3; display: grid; place-items: center;
  }
  .feed img { width: 100%; height: 100%; object-fit: contain; display: block; }
  .feed .stale { position: absolute; inset: 0; display: grid; place-items: center;
    color: var(--dim); font-size: 13px; background: #000; }
  .hud {
    position: absolute; top: 10px; left: 10px; font-size: 11px; color: var(--dim);
    background: rgba(0,0,0,.55); padding: 4px 8px; border-radius: 3px; letter-spacing: .04em;
  }
  /* 清晰度分 = hero */
  .sharp-hero { display: flex; align-items: baseline; gap: 14px; padding: 4px 2px; }
  .sharp-hero .num { font-size: 68px; font-weight: 600; line-height: 1;
    font-variant-numeric: tabular-nums; letter-spacing: -.02em; transition: color .15s; }
  .sharp-hero .lab { color: var(--dim); font-size: 12px; letter-spacing: .12em; text-transform: uppercase; }
  .sharp-bar { height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; margin-top: 6px; }
  .sharp-bar > i { display: block; height: 100%; width: 0; background: var(--good); transition: width .15s, background .15s; }
  /* 右: 控制 */
  .rail { background: var(--panel); border-left: 1px solid var(--line);
    padding: 22px 20px; display: flex; flex-direction: column; gap: 22px; }
  .rail h1 { font-size: 13px; font-weight: 600; letter-spacing: .16em; text-transform: uppercase;
    color: var(--ink); margin: 0 0 2px; }
  .rail .sub { font-size: 11px; color: var(--dim); margin: 0; line-height: 1.5; }
  .grp { display: flex; flex-direction: column; gap: 9px; }
  .grp > .t { font-size: 10px; letter-spacing: .14em; text-transform: uppercase; color: var(--dim); }
  .btns { display: grid; grid-template-columns: repeat(2,1fr); gap: 7px; }
  button {
    font-family: var(--mono); font-size: 12px; color: var(--ink);
    background: #1d222b; border: 1px solid var(--line); border-radius: 3px;
    padding: 9px 8px; cursor: pointer; transition: border-color .12s, background .12s;
  }
  button:hover { border-color: #3a4353; background: #222834; }
  button:active { background: #2a3140; }
  button.on { border-color: var(--good); color: var(--good); }
  button.af { grid-column: 1 / -1; border-color: #3a4353; }
  button.af:hover { border-color: var(--hot); color: var(--hot); }
  .readout { display: grid; grid-template-columns: auto 1fr; gap: 6px 12px; font-size: 12px; }
  .readout .k { color: var(--dim); }
  .readout .v { text-align: right; font-variant-numeric: tabular-nums; }
  .slider { display: flex; align-items: center; gap: 10px; }
  .slider input { flex: 1; accent-color: var(--hot); }
  .slider .val { width: 34px; text-align: right; font-size: 12px; color: var(--dim); font-variant-numeric: tabular-nums; }
  .note { font-size: 10px; color: var(--dim); line-height: 1.5; border-top: 1px solid var(--line); padding-top: 12px; }
  .funnel-out { border-top: 1px solid var(--line); padding-top: 10px; margin-top: 2px; }
  .verdict { font-size: 13px; font-weight: 600; padding: 6px 10px; border-radius: 3px; text-align: center; }
  .verdict.ok { background: rgba(79,209,139,.14); color: var(--good); border: 1px solid rgba(79,209,139,.4); }
  .verdict.no { background: rgba(255,92,77,.14); color: var(--hot); border: 1px solid rgba(255,92,77,.4); }
  .ftab { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 11px; }
  .ftab td, .ftab th { padding: 3px 5px; text-align: right; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
  .ftab th { color: var(--dim); font-weight: 400; text-align: right; }
  .ftab tr.best td { color: var(--good); }
  .ftab tr.bad td { color: var(--hot); opacity: .75; }
  .best-wrap { display: none; margin-top: 4px; }
  .best-wrap.show { display: block; }
  .best-wrap .lab { font-size: 11px; color: var(--dim); letter-spacing: .08em; margin-bottom: 4px; }
  .best-wrap img { width: 100%; border: 1px solid var(--good); border-radius: 4px; }
</style>
</head>
<body>
  <div class="stage">
    <div class="sharp-hero">
      <span class="num" id="sharpNum">—</span>
      <div>
        <div class="lab">Laplacian variance · 清晰度</div>
        <div class="sharp-bar"><i id="sharpBar"></i></div>
      </div>
    </div>
    <div class="feed">
      <img id="feed" alt="">
      <div class="stale" id="stale">等待画面…</div>
      <div class="hud" id="hud">—</div>
    </div>
    <div class="best-wrap" id="bestWrap">
      <div class="lab">漏斗选出的 BEST 帧 (喂给模型的那张)</div>
      <img id="bestImg" alt="">
    </div>
  </div>

  <aside class="rail">
    <div>
      <h1>相机流水线台</h1>
      <p class="sub">TCP 抓 N 帧 → 三级漏斗筛选选最优 / 或拒绝。调分辨率/对焦/旋转，观察漏斗行为。</p>
    </div>

    <div class="grp">
      <div class="t">分辨率</div>
      <div class="btns" id="resBtns">
        <button data-res="VGA">VGA · 640</button>
        <button data-res="SVGA">SVGA · 800</button>
        <button data-res="HD">HD · 1280</button>
        <button data-res="UXGA">UXGA · 1600</button>
      </div>
    </div>

    <div class="grp">
      <div class="t">对焦</div>
      <div class="btns">
        <button class="af" id="afBtn">触发单次对焦 AF</button>
      </div>
    </div>

    <div class="grp">
      <div class="t">旋转 (盲人不知朝向 · 后端真转后喂模型)</div>
      <div class="btns" id="rotBtns">
        <button data-rot="0" class="on">0°</button>
        <button data-rot="90">90°</button>
        <button data-rot="180">180°</button>
        <button data-rot="270">270°</button>
      </div>
    </div>

    <div class="grp">
      <div class="t">JPEG 质量 (小=清晰,大=省带宽)</div>
      <div class="slider">
        <input type="range" id="q" min="4" max="30" value="4">
        <span class="val" id="qv">4</span>
      </div>
    </div>

    <div class="grp">
      <div class="t">显示刷新率 (仅本地看画面, 非漏斗)</div>
      <div class="slider">
        <input type="range" id="fps" min="1" max="10" value="4">
        <span class="val" id="fpsv">4</span>
      </div>
    </div>

    <div class="grp">
      <div class="t">实时读数</div>
      <div class="readout">
        <span class="k">清晰度</span><span class="v" id="rSharp">—</span>
        <span class="k">亮度 0-255</span><span class="v" id="rBright">—</span>
        <span class="k">帧大小</span><span class="v" id="rKb">—</span>
        <span class="k">抓帧延时</span><span class="v" id="rGrab">—</span>
      </div>
    </div>

    <div class="grp">
      <div class="t">漏斗 (抓 N 帧 → 评分 → 选最优 / 对焦 / 拒绝)</div>
      <div class="t" style="margin-top:2px">标定场景 (点抓拍前先选, 会写进导出表)</div>
      <div class="btns" id="sceneBtns">
        <button data-scene="clear" class="on">清晰</button>
        <button data-scene="defocus">失焦</button>
        <button data-scene="shake">晃动</button>
        <button data-scene="blank">空白</button>
        <button data-scene="upright">正立</button>
        <button data-scene="left90">左转90</button>
        <button data-scene="right90">右转90</button>
        <button data-scene="flipped">倒置</button>
      </div>
      <div class="slider">
        <span class="k" style="font-size:11px;color:var(--dim)">N</span>
        <input type="range" id="nframes" min="3" max="10" value="5">
        <span class="val" id="nv">5</span>
      </div>
      <div class="btns">
        <button id="afToggle" class="on" style="grid-column:1/-1">自动对焦: 开 (验证对焦实效)</button>
      </div>
      <div class="btns">
        <button id="runBtn" style="grid-column:1/-1;border-color:var(--good);color:var(--good)">抓拍并跑漏斗</button>
        <button id="dlRunBtn" style="grid-column:1/-1;border-color:#3a4353">下载标定表 CSV</button>
      </div>
      <div class="t" style="margin-top:8px">录制一段过程(纯原始流, 供 rerun 看整个过程判定)</div>
      <div class="slider">
        <input type="text" id="clipName" placeholder="场景名 如 still / shake"
          style="flex:1;background:#1d222b;border:1px solid var(--line);border-radius:3px;color:var(--ink);padding:7px 8px;font-family:var(--mono);font-size:12px">
      </div>
      <div class="slider">
        <span class="k" style="font-size:11px;color:var(--dim)">时长</span>
        <input type="range" id="recDur" min="5" max="15" value="8">
        <span class="val" id="recDurv">8s</span>
      </div>
      <div class="btns">
        <button id="recClipBtn" style="grid-column:1/-1;border-color:#3a4353">开始录制</button>
      </div>
      <div class="readout" style="margin-top:2px">
        <span class="k">录制状态</span><span class="v" id="recClipState">空闲</span>
      </div>
      <div class="funnel-out" id="funnelOut" style="display:none">
        <div class="verdict" id="verdict">—</div>
        <div id="geomHint" style="display:none;margin:6px 0;padding:8px 10px;border-radius:4px;
             background:#2a2420;border:1px solid #6b5333;color:#e0b060;font-size:13px;line-height:1.5"></div>
        <div class="readout" style="margin-top:6px">
          <span class="k">选中帧</span><span class="v" id="fBest">—</span>
          <span class="k">总耗时</span><span class="v" id="fTotal">—</span>
          <span class="k">抓批</span><span class="v" id="fGrab">—</span>
          <span class="k">漏斗(筛+判+选)</span><span class="v" id="fFunnel">—</span>
        </div>
        <table class="ftab" id="ftab"></table>
      </div>
      <div class="readout" style="margin-top:2px">
        <span class="k">接受/总</span><span class="v" id="pAcc">0/0</span>
        <span class="k">拒绝原因</span><span class="v" id="pRej">—</span>
      </div>
    </div>

    <div class="grp">
      <div class="t">记录 (点上面按钮自动录 · 或手动录基线)</div>
      <div class="slider">
        <span class="k" style="font-size:11px;color:var(--dim)">自动录时长</span>
        <input type="range" id="dur" min="2" max="15" value="5">
        <span class="val" id="durv">5s</span>
      </div>
      <div class="btns">
        <button id="recBtn">手动开始记录</button>
        <button id="clearBtn">清空</button>
        <button id="dlBtn" style="grid-column:1/-1;border-color:#3a4353">下载 CSV</button>
      </div>
      <div class="readout" style="margin-top:2px">
        <span class="k">状态</span><span class="v" id="recState">空闲</span>
        <span class="k">已记录行</span><span class="v" id="recRows">0</span>
        <span class="k">段数</span><span class="v" id="recSegs">0</span>
      </div>
    </div>

    <p class="note">独占 TCP：调参时勿同时跑主程序，否则抢同一条连接。
      清晰度阈值：对焦前后、切分辨率时观察数值跳变，据此定"够清晰"的下限。</p>
  </aside>

<script>
  const $ = id => document.getElementById(id);
  let fps = 4, timer = null, dur = 5, manualOn = false;

  function colorFor(v){
    // 阈值带: 标定用. 低=模糊(红) 中(黄) 高=清晰(绿). 初值随你在真机上改.
    if (v < 60) return getComputedStyle(document.documentElement).getPropertyValue('--hot');
    if (v < 120) return getComputedStyle(document.documentElement).getPropertyValue('--warn');
    return getComputedStyle(document.documentElement).getPropertyValue('--good');
  }

  async function tick(){
    try {
      const r = await fetch('/frame?ts=' + Date.now());
      if (!r.ok) throw 0;
      const sharp = parseFloat(r.headers.get('X-Sharpness') || '0');
      const bright = parseFloat(r.headers.get('X-Brightness') || '0');
      const kb = r.headers.get('X-Kb') || '—';
      const grab = r.headers.get('X-Grab-Ms') || '—';
      const blob = await r.blob();
      $('feed').src = URL.createObjectURL(blob);
      $('stale').style.display = 'none';

      $('sharpNum').textContent = sharp.toFixed(0);
      const c = colorFor(sharp);
      $('sharpNum').style.color = c;
      const bar = $('sharpBar');
      bar.style.width = Math.min(100, sharp / 3) + '%';
      bar.style.background = c;

      $('rSharp').textContent = sharp.toFixed(1);
      $('rBright').textContent = bright.toFixed(1);
      $('rKb').textContent = kb + ' KB';
      $('rGrab').textContent = grab + ' ms';
      $('hud').textContent = 'sharp ' + sharp.toFixed(0) + '  ·  ' + kb + 'KB  ·  ' + grab + 'ms';
    } catch(e){ /* 保持上一帧 */ }
  }
  function reschedule(){ if (timer) clearInterval(timer); timer = setInterval(tick, 1000/fps); }

  document.querySelectorAll('#resBtns button').forEach(b => {
    b.onclick = async () => {
      document.querySelectorAll('#resBtns button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      await fetch('/ctl?op=res&val=' + b.dataset.res + '&dur=' + dur);
    };
  });
  $('afBtn').onclick = async () => { await fetch('/ctl?op=af&dur=' + dur); };
  document.querySelectorAll('#rotBtns button').forEach(b => {
    b.onclick = async () => {
      document.querySelectorAll('#rotBtns button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      await fetch('/ctl?op=rotate&val=' + b.dataset.rot + '&dur=' + dur);
    };
  });
  $('q').oninput = e => { $('qv').textContent = e.target.value; };
  $('q').onchange = async e => { await fetch('/ctl?op=quality&val=' + e.target.value + '&dur=' + dur); };
  $('fps').oninput = e => { fps = +e.target.value; $('fpsv').textContent = fps; reschedule(); };
  $('dur').oninput = e => { dur = +e.target.value; $('durv').textContent = dur + 's'; };

  // 记录控制
  $('recBtn').onclick = async () => {
    if (!manualOn) {
      await fetch('/rec/start?label=manual&dur=0');   // 0=手动, 直到再点停止
      manualOn = true; $('recBtn').textContent = '停止记录'; $('recBtn').classList.add('on');
    } else {
      await fetch('/rec/stop');
      manualOn = false; $('recBtn').textContent = '手动开始记录'; $('recBtn').classList.remove('on');
    }
  };
  $('clearBtn').onclick = async () => {
    await fetch('/rec/clear');
    manualOn = false; $('recBtn').textContent = '手动开始记录'; $('recBtn').classList.remove('on');
  };
  $('dlBtn').onclick = () => { window.location = '/rec/download'; };

  // 漏斗
  let nframes = 5, scene = 'clear';
  $('nframes').oninput = e => { nframes = +e.target.value; $('nv').textContent = nframes; };
  document.querySelectorAll('#sceneBtns button').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('#sceneBtns button').forEach(x => x.classList.remove('on'));
      b.classList.add('on'); scene = b.dataset.scene;
    };
  });
  let afOn = true;
  $('afToggle').onclick = async () => {
    afOn = !afOn;
    await fetch('/af_toggle?on=' + (afOn ? '1' : '0'));
    if (afOn) {
      $('afToggle').textContent = '自动对焦: 开 (验证对焦实效)';
      $('afToggle').classList.add('on');
    } else {
      $('afToggle').textContent = '自动对焦: 关 (纯决策验证/裸图)';
      $('afToggle').classList.remove('on');
    }
  };
  $('dlRunBtn').onclick = () => { window.location = '/pipeline/download'; };
  let recDur = 8;
  $('recDur').oninput = e => { recDur = +e.target.value; $('recDurv').textContent = recDur + 's'; };
  $('recClipBtn').onclick = async () => {
    const name = ($('clipName').value || '').trim();
    $('recClipBtn').disabled = true;
    $('recClipState').textContent = `录制中 ${recDur}s (走完整个过程)…`;
    $('recClipState').style.color = 'var(--hot)';
    try {
      const j = await (await fetch('/record?name=' + encodeURIComponent(name) + '&dur=' + recDur)).json();
      if (j.ok) {
        $('recClipState').textContent = `已存 ${j.name} (${j.n_frames}帧, ${j.duration_s}s)`;
        $('recClipState').style.color = 'var(--good)';
      } else {
        $('recClipState').textContent = '失败: ' + (j.err || '');
        $('recClipState').style.color = 'var(--hot)';
      }
    } catch(e){ $('recClipState').textContent = '请求失败'; }
    $('recClipBtn').disabled = false;
  };
  $('runBtn').onclick = async () => {
    $('runBtn').disabled = true; $('runBtn').textContent = '抓拍中…';
    try {
      const j = await (await fetch('/pipeline/run?n=' + nframes + '&scene=' + scene)).json();
      renderFunnel(j);
    } catch(e){ $('verdict').textContent = '请求失败'; }
    $('runBtn').disabled = false; $('runBtn').textContent = '抓拍并跑漏斗';
  };

  const REASON_CN = {
    unstable:'画面不稳(在动)—请拿稳/对准', no_content:'看不清/无内容—请靠近',
    all_blurry:'全部模糊—请拿稳', too_dark:'太暗—请到亮处', no_frames:'没抓到帧',
    need_focus:'稳但糊—已尝试对焦', aimed_wrong:'没对准—请对准药盒'
  };
  function renderFunnel(j){
    $('funnelOut').style.display = 'block';
    if (!j.ok){ $('verdict').className='verdict no'; $('verdict').textContent='失败: '+(j.err||''); return; }
    const r = j.result, v = $('verdict');
    const focusedTag = j.focused ? ' [对焦后]' : '';
    if (r.accepted){
      v.className = 'verdict ok';
      v.textContent = '✓ 选中第 ' + r.best_index + ' 帧' + focusedTag
        + (j.infer ? (' · ' + j.infer) : ' · (推理留桩)');
      $('bestWrap').classList.add('show');
      $('bestImg').src = '/pipeline/best?ts=' + Date.now();
    } else {
      v.className = 'verdict no';
      v.textContent = '✗ 拒绝: ' + (REASON_CN[r.reject_reason] || r.reject_reason) + focusedTag;
      if (j.rejected_shown){
        // 显示本次被拒批次的图 (供复盘: 系统这次面对的画面), 标注"本次被拒"
        $('bestWrap').classList.add('show');
        $('bestImg').src = '/pipeline/best?ts=' + Date.now();
      } else {
        $('bestWrap').classList.remove('show');
      }
    }
    // 方向提示 (第二步几何诊断)
    const g = j.geom || {};
    const hints = [];
    if (g.orient_hint){
      // 转向类用🔄, 确认类用❓
      const icon = (g.orient_hint.indexOf('转') >= 0) ? '🔄 ' : '❓ ';
      hints.push(icon + g.orient_hint);
    }
    // 截断提示暂不显示(真实图上二值化误报, 待带真图重做; diag字段和下游incomplete仍保留)
    // if (g.pan_hint) hints.push('↔️ 文字可能不完整：' + g.pan_hint);
    // if (g.incomplete && !g.pan_hint) hints.push('⚠️ 文字可能不完整');
    const gh = $('geomHint');
    // 方向调试信息: 总是显示分类器原始判定(raw标签+conf), 便于定位方向不准
    let orientDbg = '';
    if (r.accepted && (g.orient_raw !== undefined && g.orient_raw !== '')){
      orientDbg = '<br><span style="color:#8a7a5a;font-size:11px">'
        + '分类器raw=' + g.orient_raw + '° conf=' + (g.orient_conf ?? '—')
        + ' → state=' + (g.orient_state || '—') + '</span>';
    }
    // 状态文案: flipped(倒置)即使还没弹"请确认正反", 也要显示"检测到倒置", 别误显示成正立
    let stateLine = '';
    if (r.accepted && g.orient_state){
      if (g.orient_state === 'flipped')      stateLine = '<span style="color:var(--hot)">🔄 检测到倒置（180°）</span>';
      else if (g.orient_state === 'sideways' && !g.orient_hint) stateLine = '<span style="color:var(--hot)">检测到侧向</span>';
      else if (g.orient_state === 'upright')  stateLine = '<span style="color:#5a8a5a">方向正立</span>';
      else if (g.orient_state === 'uncertain')stateLine = '<span style="color:#8a7a5a">方向不确定（低置信）</span>';
    }
    if ((hints.length || stateLine || orientDbg) && r.accepted){
      gh.style.display = 'block';
      gh.innerHTML = (hints.length ? hints.join('<br>') : stateLine) + orientDbg;
    } else {
      gh.style.display = 'none';
    }
    const t = r.timings || {};
    $('fBest').textContent = r.accepted ? ('#' + r.best_index + focusedTag) : '—';
    $('fTotal').textContent = (t.total_ms ?? '—') + ' ms';
    $('fGrab').textContent = (t.grab_batch_ms ? t.grab_batch_ms.toFixed(0) : '—') + ' ms';
    const fsum = ((t.level1_metrics_ms||0)+(t.level2_score_ms||0)+(t.level3_decide_ms||0));
    $('fFunnel').textContent = fsum.toFixed(0) + ' ms';
    // 每帧表: quality / focus_gain / sharp / 稳定
    let html = '<tr><th>帧</th><th>quality</th><th>focus</th><th>sharp</th><th>稳定</th></tr>';
    (r.per_frame||[]).forEach((p,i) => {
      const cls = (i===r.best_index && r.accepted) ? 'best'
                : (p.quality < 0.4 ? 'bad' : '');
      html += `<tr class="${cls}"><td>${i}</td><td>${(p.quality??0).toFixed(2)}</td>`
        + `<td>${(p.focus_gain??0).toFixed(2)}</td><td>${Math.round(p.sharpness)}</td>`
        + `<td>${(p.g_stable??0).toFixed(2)}</td></tr>`;
    });
    $('ftab').innerHTML = html;
    const s = j.stats || {};
    $('pAcc').textContent = (s.accepted ?? 0) + '/' + (s.total ?? 0);
    const rr = s.reject_reasons || {};
    const parts = Object.keys(rr).map(k => (REASON_CN[k]||k).split('—')[0] + ':' + rr[k]);
    $('pRej').textContent = parts.length ? parts.join('  ') : '—';
  }

  async function pollRec(){
    try {
      const s = await (await fetch('/rec/status')).json();
      if (s.active) {
        $('recState').textContent = s.remain_s > 0
          ? ('录制中 ' + s.label + ' · 剩 ' + s.remain_s + 's')
          : ('录制中 ' + s.label);
        $('recState').style.color = 'var(--hot)';
      } else {
        $('recState').textContent = '空闲';
        $('recState').style.color = 'var(--dim)';
        // 定时录结束后, 同步手动按钮状态
        if (manualOn && s.remain_s === 0 && s.label !== 'manual') {}
      }
      $('recRows').textContent = s.n_rows;
      $('recSegs').textContent = s.n_segments;
    } catch(e){}
  }
  setInterval(pollRec, 500);

  reschedule();
</script>
</body>
</html>
"""


class PipelineStats:
    """漏斗运行统计: 接受/拒绝计数(按原因)+ best帧存盘 + 最近 best 缓存。
    拒绝率是安全指标的雏形: 该拒的拒了多少、各什么原因。"""

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.n_total = 0
        self.n_accepted = 0
        self.reasons: dict = {}       # reason -> count
        self.last_best: Optional[bytes] = None
        self._batch_i = 0
        self._runs: list[dict] = []    # 每次抓拍一行汇总 (供标定用)

    def add_run_row(self, scene: str, res, focused: bool, best_path: str = "", geom: dict = None):
        """每次抓拍追加一行汇总, 带用户标注的场景标签 + best帧路径。
        best_path: 供你事后人工看这张 best 清不清楚, 在 manual_label 列标真值 ——
        用人工判断(而非抓拍前意图标签)当 ground truth 校准阈值, 绕开标签错位。"""
        per = res.per_frame or []
        max_q = max((p.get("quality", 0) for p in per), default=0)
        max_fg = max((p.get("focus_gain", 0) for p in per), default=0)
        # best 帧的完整 CV 指标 (供标定: 一张汇总表就能对比各指标 vs ok/blur)
        bm = {}
        if 0 <= res.best_index < len(per):
            bm = per[res.best_index]
        best_sharp = bm.get("sharpness", 0)
        t = res.timings or {}
        import os as _os
        row = {
            "time": time.strftime("%H:%M:%S"),
            "scene": scene,
            "manual_label": "",             # ← 你看完 best 帧后手填: ok / blur (真值)
            "n_frames": len(per),
            "accepted": int(res.accepted),
            "need_focus": int(res.need_focus),
            "focused": int(focused),
            "reject_reason": res.reject_reason,
            "best_index": res.best_index,
            "best_sharp": round(best_sharp, 1),
            "best_local": bm.get("local_sharp", ""),
            "best_worst_block": bm.get("worst_block", ""),
            "best_block_std": bm.get("block_std", ""),
            "best_text_sharp": bm.get("text_sharp", ""),
            "best_text_ratio": bm.get("text_ratio", ""),
            "best_contrast": bm.get("contrast", ""),
            "best_dark_ratio": bm.get("dark_ratio", ""),
            "seg_stable": (res.components or {}).get("stable", ""),
            "seg_sharp": (res.components or {}).get("sharp", ""),
            "seg_content": (res.components or {}).get("content", ""),
            "seg_light": (res.components or {}).get("light", ""),
            "max_quality": round(max_q, 3),
            "max_focus_gain": round(max_fg, 3),
            "grab_batch_ms": round(t.get("grab_batch_ms", 0), 1),
            "funnel_ms": round(t.get("level1_metrics_ms", 0)
                               + t.get("level2_score_ms", 0)
                               + t.get("level3_decide_ms", 0), 1),
            "total_ms": round(t.get("total_ms", 0), 1),
            "best_file": _os.path.basename(best_path) if best_path else "",
            "best_path": best_path,
            # 方向分类器原始输出(定位方向不准: raw标签 vs 你实际摆放)
            "orient_raw": (geom or {}).get("orient_raw", ""),
            "orient_conf": (geom or {}).get("orient_conf", ""),
            "orient_state": (geom or {}).get("orient_state", ""),
            "orient_hint": (geom or {}).get("orient_hint", "") or "",
        }
        with self._lock:
            self._runs.append(row)

    def runs_csv(self) -> str:
        import csv, io
        with self._lock:
            runs = list(self._runs)
        cols = ["time", "scene", "manual_label", "n_frames", "accepted", "need_focus",
                "focused", "reject_reason", "best_index", "best_sharp",
                "best_local", "best_worst_block", "best_block_std",
                "best_text_sharp", "best_text_ratio", "best_contrast", "best_dark_ratio",
                "seg_stable", "seg_sharp", "seg_content", "seg_light",
                "max_quality", "max_focus_gain", "grab_batch_ms", "funnel_ms", "total_ms",
                "orient_raw", "orient_conf", "orient_state", "orient_hint",
                "best_file", "best_path"]
        # 未来新字段自动并入
        for r in runs:
            for k in r:
                if k not in cols:
                    cols.append(k)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in runs:
            w.writerow(r)
        return buf.getvalue()

    def save_runs_csv(self) -> Optional[Path]:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = self.out_dir / f"pipeline_runs_{stamp}.csv"
        try:
            path.write_text(self.runs_csv(), encoding="utf-8-sig")
            return path
        except Exception as e:
            print(f"[PSTATS] runs csv 存盘失败: {e}")
            return None

    def record(self, accepted: bool, reason: str):
        with self._lock:
            self.n_total += 1
            if accepted:
                self.n_accepted += 1
            else:
                self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def set_last_best(self, jpg: bytes):
        with self._lock:
            self.last_best = jpg

    def save_batch(self, frames: list[bytes], best_index: int, res):
        """存整批 N 帧 + 标出 best, 供人工核对。返回 (目录, best帧路径)。"""
        with self._lock:
            self._batch_i += 1
            bi = self._batch_i
        d = self.out_dir / f"batch_{time.strftime('%H%M%S')}_{bi:03d}"
        best_path = ""
        try:
            d.mkdir(parents=True, exist_ok=True)
            for i, jpg in enumerate(frames):
                tag = "_BEST" if i == best_index else ""
                sharp = res.per_frame[i]["sharpness"] if i < len(res.per_frame) else 0
                fp = d / f"f{i}_sharp{sharp:.0f}{tag}.jpg"
                fp.write_bytes(jpg)
                if i == best_index:
                    best_path = str(fp)
            import json
            (d / "funnel.json").write_text(
                json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return str(d), best_path
        except Exception as e:
            print(f"[PSTATS] save_batch 失败: {e}")
            return "", ""

    def summary(self) -> dict:
        with self._lock:
            rej = self.n_total - self.n_accepted
            return {
                "total": self.n_total,
                "accepted": self.n_accepted,
                "rejected": rej,
                "accept_rate": round(self.n_accepted / self.n_total, 3) if self.n_total else 0,
                "reject_reasons": dict(self.reasons),
            }


def make_app(grabber: FrameGrabber, ctl: CamControl,
             recorder: Recorder, ctl_state: dict, default_dur: float,
             funnel_cfg: "FunnelConfig", pstats: "PipelineStats") -> web.Application:
    app = web.Application()

    async def h_index(request):
        return web.Response(text=PAGE, content_type="text/html")

    async def h_frame(request):
        jpg, m, grab_ms = grabber.latest()
        if not jpg:
            return web.Response(status=503, text="no frame")
        headers = {
            "X-Sharpness": str(m.get("sharpness", 0)),
            "X-Brightness": str(m.get("brightness", 0)),
            "X-Kb": str(m.get("kb", 0)),
            "X-Grab-Ms": str(round(grab_ms, 0)),
            "Cache-Control": "no-store",
        }
        return web.Response(body=jpg, content_type="image/jpeg", headers=headers)

    async def h_ctl(request):
        op = request.query.get("op", "")
        val = request.query.get("val", "")
        dur = float(request.query.get("dur", default_dur) or default_dur)
        loop = asyncio.get_event_loop()
        label = None
        if op == "res":
            ok = await loop.run_in_executor(None, ctl.set_resolution, val)
            ctl_state["res"] = val
            label = f"res:{val}"
        elif op == "af":
            ok = await loop.run_in_executor(None, ctl.trigger_af)
            label = "af"
        elif op == "quality":
            ok = await loop.run_in_executor(None, ctl.set_quality, int(val or 10))
            label = f"quality:{val}"
        elif op == "rotate":
            grabber.set_rotation(int(val or 0))
            ctl_state["rotate"] = int(val or 0)
            ok = True
            label = f"rotate:{val}"
        else:
            return web.json_response({"ok": False, "err": "unknown op"}, status=400)
        # 控制操作自动开一段定时记录 (看该操作的效果曲线)
        if label and dur > 0:
            recorder.start_segment(label, dur)
        return web.json_response({"ok": bool(ok), "recording": label, "dur": dur})

    # ---- 记录控制 ----
    async def h_rec_start(request):
        label = request.query.get("label", "manual")
        dur = float(request.query.get("dur", 0) or 0)   # 0=手动(直到 stop)
        recorder.start_segment(label, dur)
        return web.json_response({"ok": True, **recorder.status()})

    async def h_rec_stop(request):
        recorder.stop_segment()
        return web.json_response({"ok": True, **recorder.status()})

    async def h_rec_status(request):
        return web.json_response(recorder.status())

    async def h_rec_save(request):
        path = recorder.save_disk()
        return web.json_response({"ok": path is not None,
                                  "path": str(path) if path else None,
                                  **recorder.status()})

    async def h_rec_download(request):
        csv_text = recorder.to_csv()
        recorder.save_disk()   # 下载同时也存盘一份, 双保险
        return web.Response(
            body=csv_text.encode("utf-8-sig"),
            headers={"Content-Type": "text/csv; charset=utf-8",
                     "Content-Disposition": "attachment; filename=tuning.csv"})

    async def h_rec_clear(request):
        recorder.clear()
        return web.json_response({"ok": True, **recorder.status()})

    # ---- 形态2: 抓批 -> 评分漏斗 -> (稳但糊则对焦再采) -> best/拒绝 ----
    async def h_pipeline_run(request):
        n = int(request.query.get("n", 5) or 5)
        focus_n = int(request.query.get("focus_n", 3) or 3)   # 对焦后补采几帧
        af_settle_ms = int(request.query.get("af_settle_ms", 120) or 120)  # 对焦生效等待(ms)
        #   OV5640 单次对焦(0x3022=0x03)锁定通常在~100ms量级(目标近焦时更快), 非几百ms。
        #   默认120ms为初值, 实测用 ?af_settle_ms=N 扫描找最短有效等待。
        scene = request.query.get("scene", "")                # 用户标注场景
        loop = asyncio.get_event_loop()

        def _work():
            t0 = time.monotonic()
            frames, grab_lats = grabber.grab_batch(n)
            grab_ms = (time.monotonic() - t0) * 1000
            if not frames:
                return {"ok": False, "err": "no_frames"}, None, None

            res = run_funnel(frames, funnel_cfg)
            focused = False
            af_on = ctl_state.get("af_enabled", True)
            # "自动对焦"开关 af_enabled 的语义(用户设计):
            #   ON(默认): 自主判断+按需触发对焦(路线A, 正常使用)。
            #   OFF: 不自动触发对焦, 只保留 res.need_focus 的判断结果 ——
            #        用于录制实验时验证"判据说该对焦"这个判断本身对不对, 不让对焦动作干扰。
            #   (手动"触发单次AF"按钮走 /op?op=af, 不受此开关限制, 任何时候可手动触发。)
            #   固件侧须 g_auto_af=false, 否则固件每秒硬对焦会架空此开关。
            if res.need_focus and af_on:
                tf = time.monotonic()
                ctl.trigger_af()                          # 写 /reg 0x3022=0x03 单次对焦
                # 等对焦生效再抓 —— trigger_af 后马达物理对焦需时间(OV5640单次对焦~100ms量级),
                #   立刻 grab_batch 会抓到"对焦过程中"的糊帧。af_settle_ms 可调, 实测最短有效等待。
                if af_settle_ms > 0:
                    time.sleep(af_settle_ms / 1000.0)   # _work 在 executor 线程, sleep 不阻塞事件循环
                extra, extra_lats = grabber.grab_batch(focus_n)
                res.timings["focus_af_regrab_ms"] = (time.monotonic() - tf) * 1000
                if extra:
                    frames = frames + extra
                    grab_lats = grab_lats + extra_lats
                    res = run_funnel(frames, funnel_cfg)
                    focused = True

            res.timings["grab_batch_ms"] = grab_ms
            res.timings["grab_per_frame_ms"] = [round(x, 1) for x in grab_lats]
            res.timings["focused"] = 1 if focused else 0

            infer_text = None
            # 可读性多帧一致性: 本窗口 accepted 只是"候选送下游"; 复用方向那套跨窗口累积,
            #   连续窗口多数都候选送, 才真正送下游 —— 滤掉"单窗口偶然可读"(晃动中偶抓清晰帧)。
            #   严重晃动(severe_shake)已在 run_funnel 单窗口即拒, 不进这里, 不受一致性影响。
            #   纯CV判定累积(不喂下游), 与方向一致性同机制。N=3 多数。
            rhist = ctl_state.setdefault("readable_hist", [])
            rhist.append(1 if res.accepted else 0)
            del rhist[:-3]                          # 只留最近3窗口
            readable_consistent = (len(rhist) >= 3 and sum(rhist) > 3 / 2)  # 3窗口多数
            res_dict_extra = {"readable_hist": list(rhist),
                              "readable_consistent": readable_consistent}
            if res.accepted and res.best_jpg is not None and readable_consistent:
                to = time.monotonic()
                oriented, geom_diag = process_orientation(res.best_jpg)
                res.timings["orientation_ms"] = (time.monotonic() - to) * 1000
                # 多帧一致性(方向): 累积最近若干次抓拍的"疑似倒置"标志, 连续/多数才弹"请确认正反"。
                #   纯CV判据的多帧累积(不喂VLM/OCR), 滤单帧噪声, 避免正常图偶发误报。
                if geom_diag and geom_diag.get("ok"):
                    hist = ctl_state.setdefault("flip_hist", [])
                    hist.append(1 if geom_diag.get("flipped_frame") else 0)
                    del hist[:-5]                      # 只留最近5次
                    # 5次里≥3次疑似倒置, 且本次也疑似 -> 才弹确认(多数一致)
                    if len(hist) >= 3 and sum(hist) >= 3 and geom_diag.get("flipped_frame"):
                        if not geom_diag.get("orient_hint"):
                            geom_diag["orient_hint"] = "请确认药盒正反"
                        geom_diag["flip_consistent"] = True
                    geom_diag["flip_hist"] = list(hist)
                ti = time.monotonic()
                infer_text = infer(oriented)
                res.timings["infer_ms"] = (time.monotonic() - ti) * 1000
                pstats.record(accepted=True, reason="")
                saved_dir, best_path = pstats.save_batch(frames, res.best_index, res)
                res.timings["total_ms"] = (time.monotonic() - t0) * 1000
                rd = res.to_dict(); rd.update(res_dict_extra)
                return ({"ok": True, "result": rd, "focused": focused,
                        "infer": infer_text, "geom": geom_diag, "saved_dir": saved_dir},
                        res.best_jpg, (res, focused, best_path))
            elif res.accepted and not readable_consistent:
                # 本窗口看着可读, 但连续一致性未达(可能偶然/刚开始) -> 暂不送下游, 等确认
                pstats.record(accepted=False, reason="await_consistency")
                res.timings["total_ms"] = (time.monotonic() - t0) * 1000
                rd = res.to_dict(); rd.update(res_dict_extra)
                rd["reject_reason"] = "await_consistency"
                return ({"ok": True, "result": rd, "focused": focused,
                        "infer": None, "await_consistency": True},
                        res.best_jpg, (res, focused, None))
            else:
                # 若对焦后仍 need_focus, 归为最终拒绝(对焦也没救回来)
                reason = res.reject_reason if res.reject_reason != "need_focus" else "all_blurry"
                pstats.record(accepted=False, reason=reason)
                res.reject_reason = reason
                res.timings["total_ms"] = (time.monotonic() - t0) * 1000
                # 拒绝时也存证 + 显示本次批次里最清晰的一帧(而非留旧图),
                #   让你能看到"系统这次面对的到底是什么画面", 复盘判定冤不冤。
                rej_best = res.best_index if 0 <= res.best_index < len(frames) else 0
                saved_dir, best_path = pstats.save_batch(frames, rej_best, res)
                show_jpg = frames[rej_best] if frames else None
                return ({"ok": True, "result": res.to_dict(), "focused": focused,
                        "infer": None, "geom": None, "rejected_shown": True,
                        "saved_dir": saved_dir}, show_jpg, (res, focused, best_path))

        result = await loop.run_in_executor(None, _work)
        if len(result) == 3:
            payload, best_jpg, run_info = result
        else:
            payload, best_jpg, run_info = result[0], result[1], None
        if best_jpg is not None:
            pstats.set_last_best(best_jpg)
        if run_info is not None:
            res_obj, focused, best_path = run_info
            pstats.add_run_row(scene, res_obj, focused, best_path, payload.get("geom"))
        return web.json_response({**payload, "stats": pstats.summary()})

    async def h_pipeline_best(request):
        jpg = pstats.last_best
        if not jpg:
            return web.Response(status=404)
        return web.Response(body=jpg, content_type="image/jpeg",
                           headers={"Cache-Control": "no-store"})

    async def h_pipeline_stats(request):
        return web.json_response(pstats.summary())

    async def h_pipeline_download(request):
        csv_text = pstats.runs_csv()
        pstats.save_runs_csv()   # 同时存盘
        return web.Response(
            body=csv_text.encode("utf-8-sig"),
            headers={"Content-Type": "text/csv; charset=utf-8",
                     "Content-Disposition": "attachment; filename=pipeline_runs.csv"})

    # ---- 录制一段纯原始帧流(按秒, 不掺对焦), 供 rerun 看过程判定(问题1) ----
    async def h_record(request):
        name = request.query.get("name", "").strip() or time.strftime("clip_%H%M%S")
        dur = float(request.query.get("dur", 8) or 8)
        dur = max(2.0, min(30.0, dur))
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "_-")
        clip_dir = pstats.out_dir.parent / "clips" / safe
        loop = asyncio.get_event_loop()
        try:
            rec = await loop.run_in_executor(
                None, record_stream, grabber, clip_dir, dur)
        except Exception as e:
            return web.json_response({"ok": False, "err": str(e)}, status=500)
        return web.json_response({
            "ok": True, "dir": str(rec.dir), "name": safe,
            "n_frames": len(rec.frames), "duration_s": dur,
        })

    app.router.add_get("/", h_index)
    app.router.add_get("/frame", h_frame)
    app.router.add_get("/ctl", h_ctl)
    app.router.add_get("/rec/start", h_rec_start)
    app.router.add_get("/rec/stop", h_rec_stop)
    app.router.add_get("/rec/status", h_rec_status)
    app.router.add_get("/rec/save", h_rec_save)
    app.router.add_get("/rec/download", h_rec_download)
    app.router.add_get("/rec/clear", h_rec_clear)
    app.router.add_get("/pipeline/run", h_pipeline_run)
    app.router.add_get("/pipeline/best", h_pipeline_best)
    app.router.add_get("/pipeline/stats", h_pipeline_stats)
    async def h_af_toggle(request):
        val = request.query.get("on", "")
        if val in ("1", "true", "on"):
            ctl_state["af_enabled"] = True
        elif val in ("0", "false", "off"):
            ctl_state["af_enabled"] = False
        return web.json_response({"ok": True, "af_enabled": ctl_state.get("af_enabled", True)})

    app.router.add_get("/pipeline/download", h_pipeline_download)
    app.router.add_get("/record", h_record)
    app.router.add_get("/af_toggle", h_af_toggle)
    return app


# ============================================================
# 录制 + rerun (受控对比基础)
#   录制: 采一段(未对焦) -> 触发真AF -> 再采一段(对焦后), 两段都存, 标对焦点。
#   rerun: 从录制回放帧喂 run_funnel, 同一段反复扫参数, 输入固定=受控对比。
#   解法一: 对焦效果也录进去(对焦后的帧真实存在), 故 C 出口的对焦可受控复现。
# ============================================================
class Recording:
    """一段录制: 原始帧序列 + 每帧时间戳 + 对焦触发点标记。"""

    def __init__(self, clip_dir: Path):
        self.dir = Path(clip_dir)
        self.frames: list[bytes] = []
        self.timestamps: list[float] = []
        self.af_index = -1          # 对焦触发点: 此索引及之后的帧是"对焦后"
        self.meta = {}

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for i, jpg in enumerate(self.frames):
            (self.dir / f"frame_{i:04d}.jpg").write_bytes(jpg)
        import json
        meta = {
            "n_frames": len(self.frames),
            "timestamps": self.timestamps,
            "af_index": self.af_index,     # -1=无对焦点
            **self.meta,
        }
        (self.dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.dir

    @classmethod
    def load(cls, clip_dir):
        import json
        d = Path(clip_dir)
        rec = cls(d)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        rec.af_index = meta.get("af_index", -1)
        rec.timestamps = meta.get("timestamps", [])
        rec.meta = meta
        n = meta.get("n_frames", 0)
        for i in range(n):
            fp = d / f"frame_{i:04d}.jpg"
            if fp.exists():
                rec.frames.append(fp.read_bytes())
        return rec

    def pre_af_frames(self):
        """对焦前的帧 (rerun 时先用这些跑漏斗)。"""
        if self.af_index < 0:
            return self.frames
        return self.frames[:self.af_index]

    def post_af_frames(self):
        """对焦后的帧 (rerun 时 C 出口触发对焦后回放这些)。"""
        if self.af_index < 0:
            return []
        return self.frames[self.af_index:]


def record_stream(grabber: "FrameGrabber", clip_dir: Path,
                  duration_s: float = 8.0, gap_s: float = 0.05) -> Recording:
    """按秒录一段纯原始帧流 (不掺对焦)。专为问题1: rerun 回放看整个过程的
    判定序列, 验证 CV 判的'该对焦'对不对。af_index=-1 表示无对焦点。"""
    rec = Recording(clip_dir)
    rec.af_index = -1
    t0 = time.monotonic()
    while (time.monotonic() - t0) < duration_s:
        jpg = grabber.tcp.capture()
        if jpg:
            rec.frames.append(jpg)
            rec.timestamps.append(time.monotonic() - t0)
        time.sleep(gap_s)
    rec.meta["duration_s"] = duration_s
    rec.meta["kind"] = "stream"       # 区别于对焦对比的 clip
    rec.save()
    return rec


def rerun_stream(clip_dir, cfg: "FunnelConfig" = None, n: int = 5, stride: int = 3):
    """对一段连续录制, 滑动窗口跑判定, 输出'整个过程的判定序列'。
    每 stride 帧滑一次, 窗口大小 n。看物品进->停->出时判定(A/B/C)怎么变。
    对焦命令天然关闭(回放死图), 即问题1: 验证'该对焦'的判断对不对。

    产出(供人工核对): 每窗口存 best 帧(文件名带判定) + 一个 CSV(每窗口一行,
    带判定/各分量/best帧路径)。你筛 reason=need_focus 的行, 点开原图看是否真需调焦。"""
    import csv, json
    cfg = cfg or FunnelConfig()
    rec = Recording.load(clip_dir)
    frames = rec.frames
    out_dir = Path(clip_dir) / "rerun"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== 过程判定序列 {Path(clip_dir).name} "
          f"({len(frames)}帧, ~{rec.meta.get('duration_s','?')}s, 窗口N={n} 步长={stride}) ===")
    print(f"{'win':<5}{'t(s)':<7}{'判定':<10}{'reason':<14}{'stable':<8}{'sharp':<8}{'content':<8}{'best图'}")
    print("-" * 78)
    rows = []
    i = 0
    win = 0
    flip_hist = []                 # 跨窗口多帧一致性: 累积"疑似倒置"标志
    while i + n <= len(frames):
        window = frames[i:i + n]
        res = run_funnel(window, cfg)
        t = rec.timestamps[i] if i < len(rec.timestamps) else i * 0.05
        comp = res.components or {}
        if res.accepted:
            verdict = "B_usable"
        elif res.reject_reason == "need_focus":
            verdict = "C_needfocus"
        elif res.reject_reason == "unstable":
            verdict = "A_moving"
        else:
            verdict = res.reject_reason
        # 存 best 帧 (窗口内 best_index; 拒绝时也存参考帧)
        bidx = res.best_index if 0 <= res.best_index < len(window) else 0
        best_name = f"w{win:03d}_t{t:.1f}_{verdict}.jpg"
        (out_dir / best_name).write_bytes(window[bidx])
        # best 帧的完整指标
        bm = res.per_frame[bidx] if 0 <= bidx < len(res.per_frame) else {}
        # === 方向判据 (只对被接受的 best 帧跑; 拒绝的帧朝向无意义) ===
        orient_state = ""; ud_score = ""; side_ratio = ""; orient_hint = ""; flip_consistent = ""
        if res.accepted:
            _, gd = process_orientation(window[bidx])
            if gd.get("ok"):
                orient_state = gd.get("orient_state", "")
                ud_score = gd.get("ud_score", "")
                side_ratio = gd.get("sideways_ratio", "")
                orient_hint = gd.get("orient_hint") or ""
                # 多帧一致性(按窗口顺序累积, 与 live 同逻辑): 5窗内≥3窗疑倒置且本窗也疑 -> 触发
                flip_hist.append(1 if gd.get("flipped_frame") else 0)
                del flip_hist[:-5]
                if len(flip_hist) >= 3 and sum(flip_hist) >= 3 and gd.get("flipped_frame"):
                    flip_consistent = 1
                    if not orient_hint:
                        orient_hint = "请确认药盒正反"
                else:
                    flip_consistent = 0
        print(f"{win:<5}{t:<7.1f}{verdict:<10}{res.reject_reason:<14}"
              f"{comp.get('stable',0):<8.2f}{comp.get('sharp',0):<8.2f}{comp.get('content',0):<8.2f}{best_name}")
        rows.append({
            "win": win, "t_s": round(t, 2), "verdict": verdict,
            "reject_reason": res.reject_reason, "accepted": int(res.accepted),
            "need_focus": int(res.need_focus),
            "g_stable": comp.get("stable", ""), "g_sharp": comp.get("sharp", ""),
            "g_content": comp.get("content", ""), "g_light": comp.get("light", ""),
            "seg_flow": comp.get("flow", ""),
            "best_sharp": bm.get("sharpness", ""), "best_local": bm.get("local_sharp", ""),
            "best_worst_block": bm.get("worst_block", ""), "best_text_ratio": bm.get("text_ratio", ""),
            "best_frame": best_name,
            # 方向判据输出 (供核对方向准确性)
            "orient_state": orient_state, "ud_score": ud_score,
            "sideways_ratio": side_ratio, "flip_consistent": flip_consistent,
            "orient_hint": orient_hint,
            "manual_check": "",     # ← 你看完 best 图后填: 该对焦的真该吗? ok/wrong
            "manual_orient": "",    # ← 你填这窗 best 的真实朝向: upright/sideways/flipped
        })
        i += stride
        win += 1
    # 存 CSV
    cols = ["win", "t_s", "verdict", "reject_reason", "accepted", "need_focus",
            "g_stable", "g_sharp", "g_content", "g_light", "seg_flow",
            "best_sharp", "best_local", "best_worst_block", "best_text_ratio",
            "best_frame",
            "orient_state", "ud_score", "sideways_ratio", "flip_consistent", "orient_hint",
            "manual_check", "manual_orient"]
    csv_path = out_dir / "sequence.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    from collections import Counter
    cnt = Counter(r["verdict"] for r in rows)
    print(f"\n判定分布: {dict(cnt)}")
    print(f"CSV: {csv_path}")
    print(f"best图: {out_dir}/  (筛 need_focus 的窗口, 看对应 best图是否真需调焦)")
    return rows


def record_clip(grabber: "FrameGrabber", ctl: "CamControl", clip_dir: Path,
                pre_n: int = 8, post_n: int = 8, gap_s: float = 0.05) -> Recording:
    """录一段: 抓 pre_n 帧(未对焦) -> 触发真AF -> 抓 post_n 帧(对焦后)。
    含对焦点标记, 供 rerun 复现 C 出口对焦效果 (解法一)。"""
    rec = Recording(clip_dir)
    t0 = time.monotonic()
    # 未对焦段
    for _ in range(pre_n):
        jpg = grabber.tcp.capture()
        if jpg:
            rec.frames.append(jpg)
            rec.timestamps.append(time.monotonic() - t0)
        time.sleep(gap_s)
    # 触发真 AF
    rec.af_index = len(rec.frames)
    ctl.trigger_af()
    time.sleep(0.2)   # 给对焦一点生效时间
    # 对焦后段
    for _ in range(post_n):
        jpg = grabber.tcp.capture()
        if jpg:
            rec.frames.append(jpg)
            rec.timestamps.append(time.monotonic() - t0)
        time.sleep(gap_s)
    rec.meta["pre_n"] = pre_n
    rec.meta["post_n"] = post_n
    rec.save()
    return rec


def rerun_clip(rec: "Recording", cfg: "FunnelConfig", n: int, focus_n: int = 3) -> dict:
    """对一段录制跑漏斗: 用前 n 帧(对焦前); 若判 need_focus, 用对焦后帧重评。
    输入固定, 只变 cfg/n/focus_n -> 受控对比。返回判定 + 是否用了对焦。"""
    pre = rec.pre_af_frames()
    if len(pre) < n:
        n = len(pre)
    batch = pre[:n]
    res = run_funnel(batch, cfg)
    focused = False
    if res.need_focus:
        post = rec.post_af_frames()
        if post:
            extra = post[:focus_n]
            res = run_funnel(batch + extra, cfg)   # 合并重评, best 应来自对焦后帧
            focused = True
    return {
        "accepted": res.accepted,
        "reject_reason": res.reject_reason,
        "need_focus": res.need_focus,
        "focused": focused,
        "best_index": res.best_index,
        "components": res.components,
        "n": n, "focus_n": focus_n,
    }


def rerun_sweep(clip_dir, cfg: "FunnelConfig" = None,
                n_values=(3, 5, 7), focus_values=(0, 3)):
    """对一段录制扫参数, 打印结果表 (找 N 拐点 / 对焦是否值得)。
    受控对比: 同一段帧, 只变 N 和 focus_n。"""
    cfg = cfg or FunnelConfig()
    rec = Recording.load(clip_dir)
    print(f"\n=== rerun 扫参 {Path(clip_dir).name} "
          f"({len(rec.frames)}帧, 对焦点@{rec.af_index}) ===")
    print(f"{'N':<5}{'focus_n':<9}{'判定':<12}{'用对焦':<8}{'best':<6}{'原因'}")
    print("-" * 50)
    for n in n_values:
        for fn in focus_values:
            r = rerun_clip(rec, cfg, n, fn)
            verdict = "接受" if r["accepted"] else "拒绝"
            print(f"{n:<5}{fn:<9}{verdict:<12}{'是' if r['focused'] else '否':<8}"
                  f"{r['best_index']:<6}{r['reject_reason']}")
    return rec


def main():
    ap = argparse.ArgumentParser(description="ESP32 相机流水线台 (形态2: 漏斗+录制+rerun)")
    ap.add_argument("--ip", help="眼镜 IP (live/record 模式必需)")
    ap.add_argument("--tcp-port", type=int, default=5000)
    ap.add_argument("--port", type=int, default=8080, help="网页端口")
    ap.add_argument("--record-dir", default="./tuning_logs", help="CSV/批次 存盘目录")
    ap.add_argument("--duration", type=float, default=5.0, help="点控制按钮后自动记录秒数")
    ap.add_argument("--quality", type=int, default=4, help="启动默认 JPEG 质量(小=清晰, 默认4最高)")
    # 录制 / rerun 模式
    ap.add_argument("--record", metavar="CLIP_DIR", help="录一段(含对焦点)到指定目录, 然后退出")
    ap.add_argument("--pre-n", type=int, default=8, help="录制: 对焦前帧数")
    ap.add_argument("--post-n", type=int, default=8, help="录制: 对焦后帧数")
    ap.add_argument("--rerun", metavar="CLIP_DIR", help="对一段录制扫参数(N/focus_n), 然后退出")
    args = ap.parse_args()

    # ---- rerun 模式: 纯离线, 不连眼镜 ----
    if args.rerun:
        rec = Recording.load(args.rerun)
        if rec.meta.get("kind") == "stream" or rec.af_index < 0:
            rerun_stream(args.rerun)          # 连续过程 -> 过程判定序列(问题1)
        else:
            rerun_sweep(args.rerun)           # 对焦对比 clip -> 扫参数
        return

    # ---- record 模式: 连眼镜录一段 ----
    if args.record:
        if not args.ip:
            print("record 模式需要 --ip"); return
        tcp = TCPImageClient(args.ip, args.tcp_port)
        grabber = FrameGrabber(tcp, Recorder(Path(args.record_dir)), {"res": "HD", "rotate": 0})
        grabber.start(); time.sleep(0.3)
        ctl = CamControl(args.ip)
        print(f"[REC] 录制中: {args.pre_n}帧(未对焦) -> AF -> {args.post_n}帧(对焦后)...")
        rec = record_clip(grabber, ctl, Path(args.record), args.pre_n, args.post_n)
        print(f"[REC] 已存 {rec.dir} ({len(rec.frames)}帧, 对焦点@{rec.af_index})")
        print(f"[REC] rerun: python cam_pipeline.py --rerun {rec.dir}")
        grabber.stop()
        return

    # ---- live 模式: 网页 ----
    if not args.ip:
        print("live 模式需要 --ip"); return
    recorder = Recorder(Path(args.record_dir))
    ctl_state = {"res": "HD", "rotate": 0, "af_enabled": True}
    funnel_cfg = FunnelConfig()
    pstats = PipelineStats(Path(args.record_dir) / "batches")

    tcp = TCPImageClient(args.ip, args.tcp_port)
    grabber = FrameGrabber(tcp, recorder, ctl_state)
    grabber.start()
    ctl = CamControl(args.ip)
    # 启动默认: HD + 最高画质(quality=4), 省得每次进网页手调
    ctl.set_resolution("HD"); ctl_state["res"] = "HD"
    ctl.set_quality(args.quality)
    time.sleep(0.15); ctl.trigger_af()   # 切完补一次对焦
    print(f"[PIPE] 启动默认: HD + quality={args.quality} + 对焦")

    app = make_app(grabber, ctl, recorder, ctl_state, args.duration, funnel_cfg, pstats)
    print(f"[PIPE] http://localhost:{args.port}  (TCP {args.ip}:{args.tcp_port})")
    print(f"[PIPE] 录制受控对比: python cam_pipeline.py --ip {args.ip} --record clips/case1")
    print("[PIPE] 独占 TCP: 调参时不要同时跑主程序")
    try:
        web.run_app(app, host="0.0.0.0", port=args.port, print=None)
    finally:
        grabber.stop()


if __name__ == "__main__":
    main()
