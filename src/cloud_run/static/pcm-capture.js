// AudioWorklet: microphone Float32 at the device rate -> 16-bit PCM mono at 16 kHz, posted in 40 ms chunks.
class PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.ratio = sampleRate / ((options.processorOptions && options.processorOptions.targetRate) || 16000);
    this.chunk = new Int16Array(640);  // 40 ms at 16 kHz
    this.n = 0;
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i++) {
      this.sum += channel[i];
      this.count += 1;
      this.phase += 1;
      if (this.phase >= this.ratio) {  // average the samples in each output period (cheap low-pass)
        this.phase -= this.ratio;
        const s = Math.max(-1, Math.min(1, this.sum / this.count));
        this.sum = 0;
        this.count = 0;
        this.chunk[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.n === this.chunk.length) {
          this.port.postMessage(this.chunk.buffer.slice(0));
          this.n = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCapture);
