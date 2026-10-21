// The player on a public link: the server's H.264 ladder through hls.js, or the browser's own HLS (Safari/iOS).
(() => {
  const v = document.getElementById("v");
  if (!v) return;
  const msg = document.getElementById("msg");
  const say = (text) => { msg.textContent = text; msg.hidden = !text; };
  const src = v.dataset.src;
  if (window.Hls && Hls.isSupported()) {
    const hls = new Hls({ capLevelToPlayerSize: true, maxBufferLength: 30 });
    let retries = 0;
    hls.on(Hls.Events.ERROR, (_, d) => {
      if (!d.fatal) return;
      if (d.response && d.response.code === 503) say("Too many people are watching right now. Try again in a few minutes.");
      else if (d.response && d.response.code === 404) say("This link has ended. Reload the page.");
      else if (d.type === Hls.ErrorTypes.NETWORK_ERROR && retries++ < 5) { say("The connection dropped. Retrying…"); setTimeout(() => hls.startLoad(), 3000); }
      else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
      else say("This video can't play right now. Reload the page to try again.");
    });
    hls.on(Hls.Events.FRAG_LOADED, () => { if (retries) { retries = 0; say(""); } });
    hls.loadSource(src);
    hls.attachMedia(v);
  } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
    v.src = src;
  } else {
    say("This browser can't play the video. Try Chrome, Edge, Firefox or Safari.");
  }
})();
