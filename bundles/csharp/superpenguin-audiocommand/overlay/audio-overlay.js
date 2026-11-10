/*
 * Audio overlay controller for the superpenguin-audiocommand app bundle.
 *
 * Listens on the community's alert-box live channel (SSE) for `superpenguin.audiocommand.play` frames and plays
 * `${sounds}/${file}` one sound at a time. The bundle sends only a validated bare file name; this page re-validates it and
 * builds the URL from its own `sounds` base parameter, so a frame can never point the page at another host or path.
 * Replaces PenguinTwitchBot's PlaySound sub-action page (MIT, github.com/Psychoboy/PenguinTwitchBot).
 */
(function () {
    'use strict';

    var TYPE = 'superpenguin.audiocommand.play';
    var FILE_SHAPE = /^[A-Za-z0-9_.-]{1,64}$/;
    var MAX_QUEUE = 20;
    var MAX_SEEN = 50;

    var statusEl = document.getElementById('status');
    var params = new URLSearchParams(window.location.search);
    var queue = [];
    var seen = [];
    var playing = false;

    /** Shows a configuration problem (the page is otherwise invisible). */
    function fail(text) {
        statusEl.textContent = text;
        statusEl.hidden = false;
    }

    /** True for an https base URL (or plain http on localhost) without credentials, query or fragment. */
    function validBase(base) {
        return /^https:\/\/[a-z0-9.-]+(:\d{1,5})?(\/[A-Za-z0-9_.~\/-]*)?$/i.test(base)
            || /^http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?(\/[A-Za-z0-9_.~\/-]*)?$/.test(base);
    }

    /** A bare, traversal-free audio file name. */
    function validFile(file) {
        return FILE_SHAPE.test(file) && file.indexOf('..') === -1 && file.charAt(0) !== '.' && /\.(mp3|ogg|wav)$/i.test(file);
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

    /** Plays queued sounds strictly one after another (upstream serialised overlapping sounds the same way). */
    function next() {
        if (playing || queue.length === 0) { return; }
        playing = true;
        var item = queue.shift();
        var audio = new Audio(base.replace(/\/+$/, '') + '/' + encodeURIComponent(item.file));
        audio.volume = Math.min(Math.max(item.volume / 100, 0), 1);
        var done = function () { playing = false; next(); };
        audio.onended = done;
        audio.onerror = done;
        var started = audio.play();
        if (started && typeof started.catch === 'function') { started.catch(done); }
    }

    /** Accepts one well-formed, not-yet-seen play frame. */
    function enqueue(amount) {
        if (!amount || typeof amount.file !== 'string' || typeof amount.play_id !== 'string' || !validFile(amount.file)) { return; }
        if (seen.indexOf(amount.play_id) !== -1 || queue.length >= MAX_QUEUE) { return; }
        seen.push(amount.play_id);
        if (seen.length > MAX_SEEN) { seen.shift(); }
        var volume = typeof amount.volume === 'number' ? amount.volume : 100;
        queue.push({ file: amount.file, volume: volume });
        next();
    }

    var base = params.get('sounds') || '';
    var url = liveUrl();
    if (url === null) {
        fail('Bad overlay URL parameters (origin, code, key)');
        return;
    }
    if (!validBase(base)) {
        fail('Bad sounds parameter: an https base URL of your sound files is required');
        return;
    }

    var source = new EventSource(url);
    source.onmessage = function (event) {
        try {
            var frame = JSON.parse(event.data);
            if (frame && frame.content_type === 'alert_box' && frame.alert_type === TYPE) { enqueue(frame.amount); }
        } catch (e) { /* ignore a malformed frame */ }
    };
}());
