class AssistiveAudioMirrorProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        this.frameSize = options?.processorOptions?.frameSize || 1600;
        this.buffer = new Float32Array(this.frameSize);
        this.offset = 0;
        this.active = true;
        this.port.onmessage = (event) => {
            if (event.data?.command === 'stop') this.active = false;
        };
    }

    process(inputs, outputs) {
        const output = outputs[0]?.[0];
        if (output) output.fill(0);
        if (!this.active) return true;
        const input = inputs[0]?.[0];
        if (!input) return true;
        let sourceOffset = 0;
        while (sourceOffset < input.length) {
            const count = Math.min(input.length - sourceOffset, this.frameSize - this.offset);
            this.buffer.set(input.subarray(sourceOffset, sourceOffset + count), this.offset);
            sourceOffset += count;
            this.offset += count;
            if (this.offset === this.frameSize) {
                const frame = this.buffer;
                this.port.postMessage({ type: 'audio.frame', audio: frame }, [frame.buffer]);
                this.buffer = new Float32Array(this.frameSize);
                this.offset = 0;
            }
        }
        return true;
    }
}

registerProcessor('assistive-audio-mirror', AssistiveAudioMirrorProcessor);
