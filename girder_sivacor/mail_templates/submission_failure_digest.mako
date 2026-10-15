## -*- coding: utf-8 -*-
## The daily roll-up. Counts only, and no identifiers at all: it is built from
## AlertCounter, which holds none -- see models/alerts.py. A reader who needs to
## know *which* submissions follows the link.
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>SIVACOR failure digest</title></head>
<body style="margin:0;padding:24px;background:#fafafa;color:#212121;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;line-height:1.5;">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="640" style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #e0e0e0;">
    <tr>
      <td style="padding:16px 24px;background:#37474f;color:#ffffff;">
        <div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;opacity:0.85;">SIVACOR failure digest</div>
        <div style="font-size:22px;font-weight:600;">${total} failure${'' if total == 1 else 's'}</div>
      </td>
    </tr>
    <tr>
      <td style="padding:20px 24px;">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="font-size:14px;border-collapse:collapse;">
          <tr>
            <th align="right" style="padding:4px 12px 8px 0;color:#616161;font-weight:500;width:60px;">Count</th>
            <th align="left" style="padding:4px 0 8px 0;color:#616161;font-weight:500;">Failure</th>
          </tr>
          % for row in rows:
          <tr style="border-top:1px solid #eeeeee;">
            <td align="right" style="padding:6px 12px 6px 0;font-weight:600;">${row.get('count', 0)}</td>
            <td style="padding:6px 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">
              ${row.get("code")}${' (%s)' % row.get('detail') if row.get('detail') is not None else ''}
            </td>
          </tr>
          % endfor
        </table>
      </td>
    </tr>
    <tr>
      <td style="padding:0 24px 24px 24px;font-size:13px;color:#616161;">
        Signatures already mailed once appear here with their repeat count, so a
        storm is one alert plus a number rather than one alert per submission.
        <div style="margin-top:12px;font-size:14px;"><a href="${base_url}/#sivacor/telemetry" style="color:#1565c0;">Failure telemetry</a></div>
      </td>
    </tr>
  </table>
  <div style="max-width:640px;margin:12px auto 0;font-size:11px;color:#9e9e9e;text-align:center;">SIVACOR &middot; ${current_year} &middot; sent to site administrators</div>
</body>
</html>
