// Plain JavaScript, no framework: one delegated click handler for every card, also for cards added later by a region refresh.
// Text comes from application text messages (apex.lang.getMessage), never from literals in this file.
(function () {
    'use strict';

    function text(key) {
        return apex.lang.getMessage(key);
    }

    document.addEventListener('click', function (event) {
        var button = event.target.closest('.tc-card [data-action]');
        if (!button) {
            return;
        }
        var card = button.closest('.tc-card');
        if (button.dataset.action === 'OPEN') {
            // the id comes from a data attribute written by the template; the server must check it again (never trust it)
            apex.server.process('TICKET_ACTION', { x01: 'OPEN', x02: card.dataset.ticketId }, {
                dataType: 'json',
                success: function (data) {
                    if (data && data.success) {
                        window.location.reload();      // server-side conditions (buttons, counters) are re-evaluated
                    } else {
                        apex.message.showErrors([{ type: 'error', location: 'page', message: text((data && data.key) || 'TC_ERR_ACTION'), unsafe: false }]);
                    }
                },
                error: function () {
                    apex.message.showErrors([{ type: 'error', location: 'page', message: text('TC_ERR_NETWORK'), unsafe: false }]);
                }
            });
        }
    });
})();
