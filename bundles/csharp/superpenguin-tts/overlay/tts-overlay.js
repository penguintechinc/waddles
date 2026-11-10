/*
 * TTS overlay controller for the superpenguin-tts app bundle.
 *
 * Listens on the community's alert-box live channel (SSE) for `superpenguin.tts.play` frames and plays the synthesized audio at
 * `amount.url` one clip at a time. Only https URLs (or http on localhost, for development) are played; repeated play ids are ignored.
 * Replaces PenguinTwitchBot's TTS player service (MIT, github.com/Psychoboy/PenguinTwitchBot), which had no overlay page.
 */
(function () {
    'use strict';

    var TYPE = 'superpenguin.tts.play';
    var MAX_QUEUE = 10;
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

    /** True for an https audio URL (or http on localhost) without credentials. */
    function validAudioUrl(url) {
        return /^https:\/\/[a-z0-9.-]+(:\d{1,5})?\/[^\s"'<>]*$/i.test(url)
            || /^http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?\/[^\s"'<>]*$/.test(url);
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

    /** Plays queued clips strictly one after another. */
    function next() {
        if (playing || queue.length === 0) { return; }
        playing = true;
        var audio = new Audio(queue.shift());
        var done = function () { playing = false; next(); };
        audio.onended = done;
        audio.onerror = done;
        var started = audio.play();
        if (started && typeof started.catch === 'function') { started.catch(done); }
    }

    /** Accepts one well-formed, not-yet-seen play frame. */
    function enqueue(amount) {
        if (!amount || typeof amount.url !== 'string' || typeof amount.play_id !== 'string' || !validAudioUrl(amount.url)) { return; }
        if (seen.indexOf(amount.play_id) !== -1 || queue.length >= MAX_QUEUE) { return; }
        seen.push(amount.play_id);
        if (seen.length > MAX_SEEN) { seen.shift(); }
        queue.push(amount.url);
        next();
    }

    var url = liveUrl();
    if (url === null) {
        fail('Bad overlay URL parameters (origin, code, key)');
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
