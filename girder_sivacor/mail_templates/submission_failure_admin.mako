## -*- coding: utf-8 -*-
## Operator alert. Deliberately plain: an admin triaging at 02:00 wants the
## facts in scanning order, not the branded chrome the researcher-facing
## templates carry. Everything here is read from the job, never from
## sivacor_execution_record -- see alerts._job_facts for why that matters.
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>SIVACOR submission failed: ${code}</title></head>
<body style="margin:0;padding:24px;background:#fafafa;color:#212121;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;line-height:1.5;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="640" style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #e0e0e0;">
    <tr>
      <td style="padding:16px 24px;background:${'#b71c1c' if house_pays else '#ef6c00'};color:#ffffff;">
        <div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;opacity:0.85;">SIVACOR submission failed</div>
        <div style="font-size:22px;font-weight:600;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">${code}${' (%s)' % detail if detail is not None else ''}</div>
        <div style="font-size:13px;margin-top:4px;">
          % if house_pays:
          Charged to the house &mdash; we called this our own fault.
          % else:
          Charged to the researcher.
          % endif
        </div>
      </td>
    </tr>
    <tr>
      <td style="padding:20px 24px;">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="font-size:14px;border-collapse:collapse;">
          <%
              rows = [
                  ("Step", step or "unknown"),
                  ("Job", job_id),
                  ("User", login or "unknown"),
                  ("Submission", submission_name or "unknown"),
                  ("Stages", stages if stages else "unknown"),
                  ("Elapsed", elapsed or "unknown"),
                  ("Worker size", "%s GB" % requested_memory_gb if requested_memory_gb else "default"),
                  ("Scratch disk", "%s GB" % requested_disk_gb if requested_disk_gb else "none"),
              ]
          %>
          % for label, value in rows:
          <tr>
            <td style="padding:5px 12px 5px 0;color:#616161;white-space:nowrap;vertical-align:top;width:120px;">${label}</td>
            <td style="padding:5px 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">${value}</td>
          </tr>
          % endfor
        </table>
      </td>
    </tr>
    % if log_tail:
    <tr>
      <td style="padding:0 24px 20px 24px;">
        <div style="font-size:12px;color:#616161;margin-bottom:6px;">Last log lines &mdash; this may be the only copy once the researcher deletes the submission.</div>
        <pre style="margin:0;padding:12px;background:#263238;color:#eceff1;border-radius:6px;font-size:12px;line-height:1.45;overflow-x:auto;white-space:pre-wrap;word-break:break-word;">${log_tail}</pre>
      </td>
    </tr>
    % endif
    <tr>
      <td style="padding:0 24px 24px 24px;font-size:14px;">
        <a href="${base_url}/#sivacor/telemetry" style="color:#1565c0;">Failure telemetry</a>
      </td>
    </tr>
  </table>
  <div style="max-width:640px;margin:12px auto 0;font-size:11px;color:#9e9e9e;text-align:center;">SIVACOR &middot; ${current_year} &middot; sent to site administrators</div>
</body>
</html>
