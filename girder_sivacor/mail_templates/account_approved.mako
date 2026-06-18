## -*- coding: utf-8 -*-
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="X-UA-Compatible" content="IE=edge">
    <title>SIVACOR: Account approved</title>
    <!--[if mso]>
    <style type="text/css">
        body, table, td {font-family: Arial, sans-serif !important;}
    </style>
    <![endif]-->
</head>
<body style="margin: 0; padding: 0; font-family: 'Roboto', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif; background-color: #fafafa; color: #212121; line-height: 1.5; font-size: 16px;">
    <!-- Email Container -->
    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background-color: #fafafa; padding: 24px 0;">
        <tr>
            <td align="center">
                <!-- Content Wrapper -->
                <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="600" style="max-width: 600px; background-color: #ffffff; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12), 0 1px 2px rgba(0, 0, 0, 0.24); border-radius: 12px; overflow: hidden;">

                    <!-- Header -->
                    <tr>
                        <td style="background: linear-gradient(135deg, #1976d2 0%, #1565c0 100%); padding: 32px 24px; text-align: center; position: relative;">
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                                <tr>
                                    <td align="center">
                                        <!-- Logo and Title -->
                                        <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                                            <tr>
                                                <td align="center" style="padding-bottom: 16px;">
                                                    % if logo_url:
                                                    <img src="${logo_url}" alt="SIVACOR Logo" width="60" height="60" style="display: block; border: 0; border-radius: 8px;">
                                                    % endif
                                                </td>
                                            </tr>
                                            <tr>
                                                <td align="center">
                                                    <h1 style="margin: 0; font-size: 28px; font-weight: 500; color: #ffffff; letter-spacing: 0.5px;">SIVACOR</h1>
                                                </td>
                                            </tr>
                                            <tr>
                                                <td align="center" style="padding-top: 8px;">
                                                    <p style="margin: 0; font-size: 14px; color: rgba(255, 255, 255, 0.9); line-height: 1.4;">
                                                        Scalable Infrastructure for Validation of<br>Computational Social Science Research
                                                    </p>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                    <!-- Main Content -->
                    <tr>
                        <td style="padding: 32px 24px;">
                            <h2 style="margin: 0 0 16px 0; font-size: 20px; font-weight: 500; color: #212121;">
                                Hello ${user.get('firstName')} ${user.get('lastName')},
                            </h2>
                            <p style="margin: 0 0 16px 0; color: #212121; font-size: 16px; line-height: 1.6;">
                                Your account has been approved. You may now login.
                            </p>
                            <p style="margin: 0 0 16px 0; color: #212121; font-size: 16px; line-height: 1.6;">
                                <a href="${base_url}">${base_url}</a>
                            </p>
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td style="background-color: #f5f5f5; padding: 24px; text-align: center; border-top: 1px solid #e0e0e0;">
                            <p style="margin: 0 0 8px 0; color: #757575; font-size: 12px;">
                                This is an automated notification from SIVACOR
                            </p>
                            <p style="margin: 0 0 16px 0; color: #757575; font-size: 12px;">
                                © ${current_year} SIVACOR. All rights reserved.
                            </p>
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" align="center">
                                <tr>
                                    <td style="padding: 0 8px;">
                                        <a href="${base_url}" style="color: #1976d2; text-decoration: none; font-size: 12px;">Dashboard</a>
                                    </td>
                                    <td style="padding: 0 8px; color: #e0e0e0;">|</td>
                                    <td style="padding: 0 8px;">
                                        <a href="${docs_url}" style="color: #1976d2; text-decoration: none; font-size: 12px;">Documentation</a>
                                    </td>
                                    <td style="padding: 0 8px; color: #e0e0e0;">|</td>
                                    <td style="padding: 0 8px;">
                                        <a href="mailto:support@sivacor.org" style="color: #1976d2; text-decoration: none; font-size: 12px;">Support</a>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                </table>
            </td>
        </tr>
    </table>
</body>
</html>
