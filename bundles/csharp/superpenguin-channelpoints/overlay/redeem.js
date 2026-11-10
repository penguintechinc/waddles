/*
 * Browser source for the superpenguin-channelpoints bundle. Subscribes to the community's alert-box live channel on
 * svc-presentation and shows each `superpenguin.channelpoints.redeem` message as a card.
 *
 * URL parameters (all required except duration):
 *   origin    the presentation service origin, e.g. https://overlay.example.com
 *   code      the community's 16-hex overlay code
 *   key       the overlay VIEW key
 *   duration  seconds each card stays up (2..15, default 6)
 *
 * Safety: every frame field is written with textContent (never innerHTML), no inline handlers, no third-party runtime
 * resources; unknown or malformed frames are dropped.
 */
(function () {
    'use strict';

    var ALERT_TYPE = 'superpenguin.channelpoints.redeem';
    var MAX_QUEUE = 50;
    var FADE_MS = 400;

    var statusEl = document.getElementById('status');
    var cardEl = document.getElementById('card');
    var whoEl = document.getElementById('who');
    var whatEl = document.getElementById('what');
    var inputEl = document.getElementById('input');
    var messageEl = document.getElementById('message');

    var queue = [];
    var showing = false;

    /** Sets the status pill text and colour. */
    function setStatus(text, connected) {
        statusEl.textContent = text;
        statusEl.className = connected ? 'status connected' : 'status';
        if (connected) {
            window.setTimeout(function () {
                statusEl.className = 'status connected hidden';
            }, 3000);
        }
    }

    /** Reads and validates the URL parameters; returns null (and shows why) when any is missing or malformed. */
    function readConfig() {
        var q = new URLSearchParams(window.location.search);
        var origin = q.get('origin') || '';
        var code = q.get('code') || '';
        var key = q.get('key') || '';
        var duration = parseInt(q.get('duration') || '6', 10);
        if (!/^https?:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?$/.test(origin)) {
            setStatus('Bad or missing origin parameter', false);
            return null;
        }
        if (!/^[0-9a-f]{16}$/.test(code)) {
            setStatus('Bad or missing code parameter', false);
            return null;
        }
        if (!/^[A-Za-z0-9._-]{8,256}$/.test(key)) {
            setStatus('Bad or missing key parameter', false);
            return null;
        }
        if (!(duration >= 2 && duration <= 15)) {
            duration = 6;
        }
        return { origin: origin, code: code, key: key, duration: duration * 1000 };
    }

    /** Returns the frame as a displayable record, or null when it is not a channel point redemption alert. */
    function toRedemption(frame) {
        if (!frame || frame.content_type !== 'alert_box' || frame.alert_type !== ALERT_TYPE) {
            return null;
        }
        var amount = frame.amount && typeof frame.amount === 'object' ? frame.amount : {};
        return {
            who: typeof frame.display_name === 'string' && frame.display_name ? frame.display_name : 'Someone',
            reward: typeof amount.reward === 'string' ? amount.reward : '',
            cost: typeof amount.cost === 'number' ? amount.cost : null,
            input: typeof amount.input === 'string' ? amount.input : '',
            message: typeof frame.message === 'string' ? frame.message : ''
        };
    }

    /** Shows the next queued redemption, then hides the card and moves on. */
    function pump(durationMs) {
        if (showing || queue.length === 0) {
            return;
        }
        showing = true;
        var r = queue.shift();
        whoEl.textContent = r.who;
        whatEl.textContent = r.reward ? 'redeemed ' + r.reward + (r.cost === null ? '' : ' (' + r.cost + ')') : 'redeemed a reward';
        inputEl.textContent = r.input;
        messageEl.textContent = r.message;
        cardEl.className = 'card';
        window.setTimeout(function () {
            cardEl.className = 'card hidden';
            window.setTimeout(function () {
                showing = false;
                pump(durationMs);
            }, FADE_MS);
        }, durationMs);
    }

    /** Opens the live channel; EventSource reconnects by itself, the pill reflects the state. */
    function connect(cfg) {
        var url = cfg.origin + '/' + cfg.code + '/alert_box/live?key=' + encodeURIComponent(cfg.key);
        var source = new EventSource(url);
        source.onopen = function () {
            setStatus('Connected', true);
        };
        source.onerror = function () {
            setStatus('Disconnected - retrying', false);
        };
        source.onmessage = function (event) {
            var frame;
            try {
                frame = JSON.parse(event.data);
            } catch (ignored) {
                return;
            }
            var redemption = toRedemption(frame);
            if (redemption === null) {
                return;
            }
            if (queue.length >= MAX_QUEUE) {
                queue.shift();
            }
            queue.push(redemption);
            pump(cfg.duration);
        };
    }

    var config = readConfig();
    if (config !== null) {
        connect(config);
    }
}());
