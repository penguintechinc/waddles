/*
 * Browser-source overlay for the SuperPenguinTV giveaway/raffle app bundle (original work, MIT).
 * URL query: origin (presentation service base URL), code (overlay code), key (VIEW key).
 * It listens to the community's `alert_box` live channel and renders only `superpenguin.giveaway.*` and
 * `superpenguin.raffle.*` alerts. All text is written with textContent; nothing is ever parsed as HTML.
 */
(function () {
  'use strict';

  var SHOW_MILLIS = 15000;
  var GIVEAWAY = 'superpenguin.giveaway.';
  var RAFFLE = 'superpenguin.raffle.';
  var hideTimer = null;

  /** Reads and validates the page's query parameters; returns null when anything is missing or unsafe. */
  function readConfig(search) {
    var params = new URLSearchParams(search);
    var origin = params.get('origin');
    var code = params.get('code');
    var key = params.get('key');
    if (!origin || !code || !key) { return null; }
    if (!/^[0-9a-f]{16}$/.test(code)) { return null; }
    var url;
    try { url = new URL(origin); } catch (e) { return null; }
    var local = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
    if (url.protocol !== 'https:' && !(local && url.protocol === 'http:')) { return null; }
    return { origin: url.origin, code: code, key: key };
  }

  /** Maps one alert frame to the three display lines, or null for an alert this overlay does not render. */
  function describe(frame) {
    if (!frame || frame.content_type !== 'alert_box' || typeof frame.alert_type !== 'string') { return null; }
    var type = frame.alert_type;
    var amount = frame.amount && typeof frame.amount === 'object' ? frame.amount : {};
    var name = typeof frame.display_name === 'string' && frame.display_name ? frame.display_name : null;
    if (type === GIVEAWAY + 'open') {
      return { title: 'Giveaway open!', line1: 'Prize: ' + String(amount.prize || ''), line2: 'Type !tgw enter AMOUNT to join' };
    }
    if (type === GIVEAWAY + 'closed') {
      return {
        title: 'Entries closed',
        line1: String(amount.entrants || 0) + ' entrants, ' + String(amount.tickets || 0) + ' tickets',
        line2: 'Commitment: ' + String(amount.commitment || '')
      };
    }
    if (type === GIVEAWAY + 'winner') {
      return {
        title: 'Winner!',
        line1: (name || 'A winner') + ' wins ' + String(amount.prize || ''),
        line2: 'Chance ' + String(amount.chance || '?') + '% - seed commitment ' + String(amount.commitment || '')
      };
    }
    if (type === RAFFLE + 'winner') {
      var winners = Array.isArray(amount.winners) ? amount.winners.length : 0;
      return {
        title: 'Raffle winners!',
        line1: name ? name + ' wins' : String(winners) + ' winner(s) drawn',
        line2: 'Raffle ' + String(amount.key || '') + ' - ' + String(amount.each || 0) + ' each'
      };
    }
    return null;
  }

  /** Writes the lines into the card with textContent and schedules the hide. */
  function show(lines) {
    document.getElementById('title').textContent = lines.title;
    document.getElementById('line1').textContent = lines.line1;
    document.getElementById('line2').textContent = lines.line2;
    var card = document.getElementById('card');
    card.classList.add('visible');
    if (hideTimer !== null) { clearTimeout(hideTimer); }
    hideTimer = setTimeout(function () { card.classList.remove('visible'); hideTimer = null; }, SHOW_MILLIS);
  }

  function setStatus(text) { document.getElementById('status').textContent = text; }

  function start() {
    var config = readConfig(window.location.search);
    if (!config) {
      setStatus('Missing or invalid ?origin=&code=&key= parameters');
      return;
    }
    var url = config.origin + '/' + config.code + '/alert_box/live?key=' + encodeURIComponent(config.key);
    var source = new EventSource(url);
    source.onopen = function () { setStatus('connected'); };
    source.onerror = function () { setStatus('reconnecting...'); };
    source.onmessage = function (event) {
      var frame;
      try { frame = JSON.parse(event.data); } catch (e) { return; }
      var lines = describe(frame);
      if (lines) { show(lines); }
    };
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { readConfig: readConfig, describe: describe };
  } else {
    start();
  }
}());
