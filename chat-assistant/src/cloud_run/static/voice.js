// Live voice with the assistant: mic -> /ws/voice -> Gemini Live -> speaker, with a live transcript.
"use strict";

(function () {
  const A = window.companyAssistant;
  const $ = (id) => document.getElementById(id);
  const OUT_RATE = 24000;

  let call = null;

  class VoiceCall {
    constructor() {
      this.muted = false;
      this.ready = false;
      this.playHead = 0;
      this.sources = new Set();
      this.live = { you: "", assistant: "" };   // current turn's running transcript
      this.lines = [];                          // finished lines shown this call
    }

    async start() {
      setState("Connecting…");
      $("voicebar").hidden = false;
      $("startVoice").disabled = true;
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      this.inCtx = new AudioContext();
      await this.inCtx.audioWorklet.addModule("/static/pcm-capture.js");
      this.node = new AudioWorkletNode(this.inCtx, "pcm-capture", { processorOptions: { targetRate: 16000 } });
      this.inCtx.createMediaStreamSource(this.stream).connect(this.node);
      this.node.port.onmessage = (e) => this.onMic(e.data);
      this.outCtx = new AudioContext({ sampleRate: OUT_RATE });

      const scheme = location.protocol === "https:" ? "wss" : "ws";
      this.ws = new WebSocket(`${scheme}://${location.host}/ws/voice`);
      this.ws.binaryType = "arraybuffer";
      this.ws.onmessage = (e) => (typeof e.data === "string" ? this.onJson(JSON.parse(e.data)) : this.play(e.data));
      this.ws.onclose = () => this.end(false);
      this.ws.onerror = () => A.toast("Voice connection failed");
    }

    onMic(buffer) {
      const pcm = new Int16Array(buffer);
      let peak = 0;
      for (let i = 0; i < pcm.length; i += 8) peak = Math.max(peak, Math.abs(pcm[i]));
      $("vmeter").style.width = `${Math.min(100, Math.round((peak / 12000) * 100))}%`;
      if (this.ready && !this.muted && this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(buffer);
    }

    onJson(msg) {
      if (msg.type === "ready") { this.ready = true; setState("Listening. Go ahead and talk"); }
      else if (msg.type === "transcript") {
        this.live[msg.who] += msg.text;
        if (msg.who === "assistant") setState("Speaking…");
        this.renderLive();
      } else if (msg.type === "tool") setState(`Working: ${msg.name.replace(/_/g, " ")}…`);
      else if (msg.type === "interrupted") { this.stopPlayback(); setState("Listening…"); }
      else if (msg.type === "turn_complete") {
        for (const who of ["you", "assistant"]) {
          if (this.live[who].trim()) this.lines.push({ role: who === "you" ? "user" : "assistant", text: this.live[who].trim(), at: new Date().toISOString(), voice: true });
          this.live[who] = "";
        }
        this.renderLive();
        setState("Listening…");
        A.refreshKnowledge();
      } else if (msg.type === "error") { A.toast(msg.message); setState(msg.message); }
    }

    renderLive() {
      const extra = this.lines.map((t) => A.turnNode(t));
      if (this.live.you.trim()) extra.push(A.turnNode({ role: "user", text: this.live.you.trim() }, true));
      if (this.live.assistant.trim()) extra.push(A.turnNode({ role: "assistant", text: this.live.assistant.trim() }, true));
      A.renderThread(extra);
    }

    play(buffer) {
      const pcm = new Int16Array(buffer);
      if (!pcm.length) return;
      const audio = this.outCtx.createBuffer(1, pcm.length, OUT_RATE);
      const data = audio.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 0x8000;
      const src = this.outCtx.createBufferSource();
      src.buffer = audio;
      src.connect(this.outCtx.destination);
      const at = Math.max(this.outCtx.currentTime + 0.02, this.playHead);  // queue back-to-back, no gaps
      src.start(at);
      this.playHead = at + audio.duration;
      this.sources.add(src);
      src.onended = () => this.sources.delete(src);
    }

    stopPlayback() {
      for (const s of this.sources) { try { s.stop(); } catch { /* already stopped */ } }
      this.sources.clear();
      this.playHead = 0;
    }

    toggleMute() {
      this.muted = !this.muted;
      $("vmute").textContent = this.muted ? "Unmute" : "Mute";
      setState(this.muted ? "Muted" : "Listening…");
    }

    end(sendStop = true) {
      if (this.ended) return;
      this.ended = true;
      try { if (sendStop && this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "stop" })); } catch { /* closing */ }
      try { this.ws && this.ws.close(); } catch { /* closing */ }
      this.stopPlayback();
      if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
      try { this.inCtx && this.inCtx.close(); } catch { /* closed */ }
      try { this.outCtx && this.outCtx.close(); } catch { /* closed */ }
      $("voicebar").hidden = true;
      $("startVoice").disabled = false;
      $("vmute").textContent = "Mute";
      call = null;
      setTimeout(() => A.loadTalk(), 800);  // the server saved the finished turns into the conversation
    }
  }

  function setState(text) { $("vstate").textContent = text; }

  $("startVoice").onclick = async () => {
    if (call) return;
    if (A.partner !== "assistant") { A.toast("Voice works with the Assistant"); return; }
    if (!navigator.mediaDevices || !window.AudioWorkletNode) { A.toast("This browser can't do live voice. Try Chrome or Edge."); return; }
    call = new VoiceCall();
    try { await call.start(); }
    catch (e) { A.toast(e.name === "NotAllowedError" ? "Allow the microphone to talk to the assistant" : e.message); call && call.end(false); }
  };
  $("vend").onclick = () => call && call.end();
  $("vmute").onclick = () => call && call.toggleMute();
  window.addEventListener("beforeunload", () => call && call.end());
})();
