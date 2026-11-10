/*
 * Wheel overlay controller for the superpenguin-wheel app bundle.
 *
 * Adapted from PenguinTwitchBot wwwroot/js/wheel.js (MIT, github.com/Psychoboy/PenguinTwitchBot): the wheel properties and the
 * spin-wheel usage follow upstream. Differences: the page reads the community's alert-box live channel (SSE) instead of a
 * bot-owned websocket, it never reports a result back (the bundle already chose and recorded the winner), every string is
 * rendered with textContent, the tick sound is synthesized with WebAudio, and no third-party host is contacted.
 */
(function () {
    'use strict';

    var TYPE_PREFIX = 'superpenguin.wheel.';
    var MAX_ITEMS = 24;
    var MAX_LABEL = 24;

    var statusEl = document.getElementById('status');
    var stageEl = document.getElementById('stage');
    var wheelEl = document.getElementById('wheel');
    var winnerEl = document.getElementById('winner');

    var params = new URLSearchParams(window.location.search);
    var wheel = null;
    var labels = [];
    var lastSpinId = '';
    var audioCtx = null;

    /** Shows the connection state; a connected badge fades after a moment. */
    function setStatus(text, connected) {
        statusEl.textContent = text;
        statusEl.className = 'status' + (connected ? ' connected' : '');
        if (connected) {
            window.setTimeout(function () { statusEl.className = 'status connected hidden'; }, 2500);
        }
    }

    /** Validates the origin/code/key query parameters; returns the live-channel URL or null. */
    function liveUrl() {
        var origin = params.get('origin') || '';
        var code = params.get('code') || '';
        var key = params.get('key') || '';
        var originOk = /^https:\/\/[a-z0-9.-]+(:\d{1,5})?$/i.test(origin) || /^http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?$/.test(origin);
        if (!originOk || !/^[0-9a-f]{16}$/.test(code) || !/^[A-Za-z0-9_.~-]{1,512}$/.test(key)) {
            return null;
        }
        return origin + '/' + code + '/alert_box/live?key=' + encodeURIComponent(key);
    }

    /** A short click: the upstream tick sample is not shipped, so a 25 ms WebAudio blip stands in for it. */
    function tick() {
        try {
            audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
            if (audioCtx.state === 'suspended') { audioCtx.resume(); }
            var osc = audioCtx.createOscillator();
            var gain = audioCtx.createGain();
            osc.type = 'square';
            osc.frequency.value = 880;
            gain.gain.setValueAtTime(0.08, audioCtx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.025);
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start();
            osc.stop(audioCtx.currentTime + 0.03);
        } catch (e) { /* audio is cosmetic; the animation still plays */ }
    }

    /** Removes the wheel and winner banner. */
    function hideWheel() {
        if (wheel) { wheel.remove(); wheel = null; }
        labels = [];
        winnerEl.hidden = true;
        winnerEl.textContent = '';
    }

    /** Keeps only well-formed items: bounded count, string labels, positive numeric weights. */
    function cleanItems(raw) {
        var items = [];
        if (!Array.isArray(raw)) { return items; }
        raw.slice(0, MAX_ITEMS).forEach(function (item) {
            if (item && typeof item.label === 'string' && typeof item.weight === 'number' && item.weight > 0) {
                items.push({ label: item.label.slice(0, MAX_LABEL), weight: item.weight });
            }
        });
        return items;
    }

    /** Builds the wheel (upstream properties) from a `wheel.show` frame. */
    function showWheel(amount) {
        hideWheel();
        var items = cleanItems(amount && amount.items);
        if (items.length < 2) { return; }
        labels = items.map(function (i) { return i.label; });
        var overlay = new Image();
        overlay.src = './img/wheel.svg';
        wheel = new spinWheel.Wheel(wheelEl, {
            items: items,
            itemLabelColors: ['#fff'],
            itemLabelBaselineOffset: -0.07,
            radius: 0.84,
            itemLabelRadius: 0.93,
            itemLabelRadiusMax: 0.35,
            itemBackgroundColors: ['#ffc93c', '#66bfbf', '#a2d5f2', '#515070', '#43658b', '#ed6663', '#d54062'],
            rotationSpeedMax: 500,
            rotationResistance: -100,
            lineWidth: 1,
            lineColor: '#fff',
            itemLabelFont: 'Rubik',
            overlayImage: overlay
        });
        wheel.onCurrentIndexChange = tick;
        wheel.onRest = function (e) {
            var label = labels[e.currentIndex];
            if (typeof label === 'string') {
                winnerEl.textContent = label;
                winnerEl.hidden = false;
            }
        };
    }

    /** Spins to the index the bundle chose; a repeated spin id (a redelivered event) is ignored. */
    function spin(amount) {
        if (!wheel || !amount || typeof amount.spin_to !== 'number' || typeof amount.spin_id !== 'string') { return; }
        if (amount.spin_id === lastSpinId) { return; }
        if (amount.spin_to < 0 || amount.spin_to >= labels.length) { return; }
        lastSpinId = amount.spin_id;
        winnerEl.hidden = true;
        var duration = typeof amount.duration_ms === 'number' ? Math.min(Math.max(amount.duration_ms, 1000), 15000) : 10000;
        wheel.spinToItem(amount.spin_to, duration, false, 5);
    }

    /** Routes one live frame; anything that is not this bundle's alert type is ignored. */
    function handle(frame) {
        if (!frame || frame.content_type !== 'alert_box' || typeof frame.alert_type !== 'string') { return; }
        if (frame.alert_type.indexOf(TYPE_PREFIX) !== 0) { return; }
        switch (frame.alert_type.slice(TYPE_PREFIX.length)) {
            case 'show': showWheel(frame.amount); break;
            case 'spin': spin(frame.amount); break;
            case 'hide': hideWheel(); break;
            default: break;
        }
    }

    if (params.get('backdrop') === '1') { stageEl.className = 'stage backdrop'; }

    var url = liveUrl();
    if (url === null) {
        setStatus('Bad overlay URL parameters (origin, code, key)', false);
        return;
    }

    var source = new EventSource(url);
    source.onopen = function () { setStatus('Connected', true); };
    source.onerror = function () { setStatus('Disconnected - retrying', false); };
    source.onmessage = function (event) {
        try { handle(JSON.parse(event.data)); } catch (e) { /* ignore a malformed frame */ }
    };
}());
