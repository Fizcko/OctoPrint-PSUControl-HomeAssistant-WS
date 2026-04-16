$(function() {
    $(document).on('click', '#psucontrol_hass_ws_test_btn', function() {
        var $btn = $(this);
        var $result = $('#psucontrol_hass_ws_test_result');
        $btn.prop('disabled', true);
        $result.html('<span class="muted"><i class="fa fa-spinner fa-spin"></i> Testing...</span>');

        OctoPrint.simpleApiCommand('psucontrol_hass_ws', 'test', {})
            .done(function(r) {
                var html = '<ul class="unstyled" style="margin: 8px 0;">';
                if (r && r.checks) {
                    r.checks.forEach(function(c) {
                        var cls = c.ok ? 'label-success' : 'label-important';
                        var txt = c.ok ? 'OK' : 'FAIL';
                        html += '<li style="margin: 4px 0;">'
                             + '<span class="label ' + cls + '">' + txt + '</span> '
                             + '<b>' + $('<div>').text(c.name).html() + '</b>: '
                             + $('<div>').text(c.detail).html()
                             + '</li>';
                    });
                }
                html += '</ul>';
                if (r && r.ok) {
                    html = '<div class="alert alert-success" style="margin-bottom: 0;">All checks passed.</div>' + html;
                } else {
                    html = '<div class="alert alert-error" style="margin-bottom: 0;">One or more checks failed.</div>' + html;
                }
                $result.html(html);
            })
            .fail(function(xhr) {
                var detail = (xhr && (xhr.responseText || xhr.statusText)) || 'Unknown error';
                $result.html('<div class="alert alert-error">Test request failed: '
                             + $('<div>').text(detail).html() + '</div>');
            })
            .always(function() {
                $btn.prop('disabled', false);
            });
    });
});
