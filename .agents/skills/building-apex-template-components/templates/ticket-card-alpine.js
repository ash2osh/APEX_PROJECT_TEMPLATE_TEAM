// OPTIONAL: the same card driven by Alpine.js. Use it only when a card needs its own state (expand/collapse, busy flag, per-card buttons).
// Load order matters: this file must be listed BEFORE Alpine in the plug-in's javaScript fileUrls, so the listener below exists when Alpine starts.
(function () {
    'use strict';

    function text(key) {
        return apex.lang.getMessage(key);
    }

    document.addEventListener('alpine:init', function () {
        Alpine.data('ticketCard', function (config) {
            return {
                ticketId: config.ticketId,
                status: config.status,
                open: false,
                loading: false,
                t: function (key) { return text(key); },          // use as x-text="t('TC_OPEN')" in the partial
                canOpen: function () { return this.status === 'OPEN'; },
                openTicket: function () {
                    var self = this;
                    self.loading = true;
                    apex.server.process('TICKET_ACTION', { x01: 'OPEN', x02: self.ticketId }, {
                        dataType: 'json',
                        success: function (data) { self.loading = false; if (data && data.success) { window.location.reload(); } },
                        error: function () { self.loading = false; }
                    });
                }
            };
        });
    });
})();
// In the partial:  <article class="tc-card" x-data="ticketCard({ticketId:'#TICKET_ID#',status:'#STATUS_CODE#'})"> ... <button type="button" :disabled="loading" x-show="canOpen()" @click="openTicket()">
// In the plug-in:  javaScript { fileUrls: [ #APP_FILES#ticket-card-alpine.js   https://cdn.jsdelivr.net/npm/alpinejs@3.14.9/dist/cdn.min.js ] }   (pin the version; or store Alpine as a static file)
