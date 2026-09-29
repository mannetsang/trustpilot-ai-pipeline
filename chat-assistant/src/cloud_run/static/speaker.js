// Read replies aloud: the 🔊 button on each reply, and the "Read replies aloud" switch for new ones.
"use strict";

(function () {
  const A = window.companyAssistant;
  let current = null;  // what's being read: { button, ctx, reader, cancelled, wake }

  function setButton(button, playing) {
    if (!button) return;
    button.textContent = playing ? "■" : "🔊";
    button.title = playing ? "Stop reading" : "Read aloud";
    button.setAttribute("aria-label", button.title);
    button.classList.toggle("on", playing);
  }

  function stop() {
    const me = current;
    if (!me) return;
    current = null;
    me.cancelled = true;
    try { me.reader && me.reader.cancel(); } catch { /* already closed */ }
    try { me.ctx && me.ctx.close(); } catch { /* already closed */ }
    if (me.wake) me.wake();
    setButton(me.button, false);
  }

  // The server streams 16-bit mono PCM as it's generated; each piece is queued right behind the last,
  // so speech starts with the first piece (1-2 s) instead of after the whole reply is made.
  async function play(text, partner, button) {
    stop();
    const me = { button, ctx: null, reader: null, cancelled: false, wake: null };
    current = me;
    setButton(button, true);
    try {
      const resp = await fetch("/api/speak", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Chat-Assistant": "1" },
        body: JSON.stringify({ partner, text }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error || `Couldn't read it out (${resp.status})`);
      }
      if (me.cancelled) return;
      const rate = Number(resp.headers.get("X-Sample-Rate") || 24000);
      me.ctx = new AudioContext({ sampleRate: rate });
      if (me.ctx.state === "suspended") await me.ctx.resume();
      me.reader = resp.body.getReader();
      let playHead = me.ctx.currentTime + 0.3, carry = null;  // OpenAI audio comes in bursts up to 0.2 s late
      for (;;) {
        const { value, done } = await me.reader.read();
        if (done || me.cancelled) break;
        let bytes = value;
        if (carry) { bytes = new Uint8Array(carry.length + value.length); bytes.set(carry); bytes.set(value, carry.length); }
        const even = bytes.length - (bytes.length % 2);  // network chunks can split a sample in two
        carry = even < bytes.length ? bytes.slice(even) : null;
        if (!even) continue;
        const pcm = new Int16Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + even));
        const audio = me.ctx.createBuffer(1, pcm.length, rate);
        const data = audio.getChannelData(0);
        for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 0x8000;
        const src = me.ctx.createBufferSource();
        src.buffer = audio;
        src.connect(me.ctx.destination);
        playHead = Math.max(playHead, me.ctx.currentTime + 0.05);  // after a stall, resume from now
        src.start(playHead);
        playHead += audio.duration;
      }
      if (me.cancelled) return;
      await new Promise((resolve) => {  // let the queued audio finish
        me.wake = resolve;
        setTimeout(resolve, Math.max(0, (playHead - me.ctx.currentTime) * 1000) + 100);
      });
    } catch (e) {
      if (!me.cancelled) A.toast(e.message);
    } finally {
      if (current === me) stop();
    }
  }

  function toggle(text, partner, button) {
    if (current && current.button === button) stop();
    else play(text, partner, button);
  }

  let auto = false;
  try { auto = localStorage.getItem("readAloud") === "1"; } catch { /* storage unavailable */ }

  window.companySpeaker = {
    play, stop, toggle,
    get auto() { return auto; },
    set auto(on) {
      auto = !!on;
      try { localStorage.setItem("readAloud", auto ? "1" : "0"); } catch { /* storage unavailable */ }
      if (!auto) stop();
    },
  };
})();
