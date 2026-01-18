## -*- coding: utf-8 -*-
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="X-UA-Compatible" content="IE=edge">
    <title>SIVACOR Submission ${status_text}</title>
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

                    <!-- Status Banner -->
                    <tr>
                        <td style="padding: 0;">
                            % if is_success:
                            <div style="background-color: #4caf50; padding: 16px 24px; text-align: center;">
                                <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                                    <tr>
                                        <td align="center">
                                            <span style="display: inline-block; width: 24px; height: 24px; background-color: rgba(255, 255, 255, 0.3); border-radius: 50%; line-height: 24px; vertical-align: middle; margin-right: 8px; font-size: 16px; color: #ffffff;">✓</span>
                                            <span style="color: #ffffff; font-size: 18px; font-weight: 500; vertical-align: middle;">Submission Completed Successfully</span>
                                        </td>
                                    </tr>
                                </table>
                            </div>
                            % else:
                            <div style="background-color: #f44336; padding: 16px 24px; text-align: center;">
                                <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                                    <tr>
                                        <td align="center">
                                            <span style="display: inline-block; width: 24px; height: 24px; background-color: rgba(255, 255, 255, 0.3); border-radius: 50%; line-height: 24px; vertical-align: middle; margin-right: 8px; font-size: 16px; color: #ffffff;">✕</span>
                                            <span style="color: #ffffff; font-size: 18px; font-weight: 500; vertical-align: middle;">Submission Failed</span>
                                        </td>
                                    </tr>
                                </table>
                            </div>
                            % endif
                        </td>
                    </tr>

                    <!-- Main Content -->
                    <tr>
                        <td style="padding: 32px 24px;">
                            <h2 style="margin: 0 0 16px 0; font-size: 20px; font-weight: 500; color: #212121;">
                                Hello ${user_name},
                            </h2>

                            % if is_success:
                            <p style="margin: 0 0 16px 0; color: #212121; font-size: 16px; line-height: 1.6;">
                                Your computational research job has completed successfully. The results are now available for download.
                            </p>
                            % else:
                            <p style="margin: 0 0 16px 0; color: #212121; font-size: 16px; line-height: 1.6;">
                                Unfortunately, your computational research job encountered an error during execution. Please review the error details below.
                            </p>
                            % endif

                            <!-- Job Details Card -->
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background-color: #f5f5f5; border-radius: 8px; margin: 24px 0;">
                                <tr>
                                    <td style="padding: 20px;">
                                        <h3 style="margin: 0 0 16px 0; font-size: 16px; font-weight: 500; color: #1976d2;">Job Details</h3>

                                        <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                                            <tr>
                                                <td style="padding: 8px 0; color: #757575; font-size: 14px; width: 140px;">Job ID:</td>
                                                <td style="padding: 8px 0; color: #212121; font-size: 14px; font-family: 'Courier New', monospace; word-break: break-all;">${job_id}</td>
                                            </tr>
                                            % if submission_time:
                                            <tr>
                                                <td style="padding: 8px 0; color: #757575; font-size: 14px;">Submitted:</td>
                                                <td style="padding: 8px 0; color: #212121; font-size: 14px;">${submission_time}</td>
                                            </tr>
                                            % endif
                                            % if completion_time:
                                            <tr>
                                                <td style="padding: 8px 0; color: #757575; font-size: 14px;">Completed:</td>
                                                <td style="padding: 8px 0; color: #212121; font-size: 14px;">${completion_time}</td>
                                            </tr>
                                            % endif
                                            % if execution_time:
                                            <tr>
                                                <td style="padding: 8px 0; color: #757575; font-size: 14px;">Execution Time:</td>
                                                <td style="padding: 8px 0; color: #212121; font-size: 14px;">${execution_time}</td>
                                            </tr>
                                            % endif
                                            <tr>
                                                <td style="padding: 8px 0; color: #757575; font-size: 14px;">Status:</td>
                                                <td style="padding: 8px 0;">
                                                    <span style="display: inline-block; padding: 4px 12px; border-radius: 16px; font-size: 12px; font-weight: 500;
                                                        % if is_success:
                                                        background-color: #e8f5e9; color: #2e7d32;
                                                        % else:
                                                        background-color: #ffebee; color: #c62828;
                                                        % endif
                                                    ">${status_text}</span>
                                                </td>
                                            </tr>
                                            % if stages:
                                            <tr>
                                                <td style="padding: 8px 0; color: #757575; font-size: 14px; vertical-align: top;">Stages:</td>
                                                <td style="padding: 8px 0; color: #212121; font-size: 14px;">
                                                    <ul style="margin: 0; padding-left: 20px; list-style-type: disc;">
                                                        % for stage in stages:
                                                        <li>${stage['main_file']} - ${stage['image_name']}:${stage['image_tag']}</li>
                                                        % endfor
                                                    </ul>
                                                </td>
                                            </tr>
                                            % endif
                                        </table>
                                    </td>
                                </tr>
                            </table>

                            % if is_success:
                            <!-- Success Actions -->
                            <div style="margin: 24px 0;">
                                <h3 style="margin: 0 0 16px 0; font-size: 16px; font-weight: 500; color: #212121;">Available Downloads</h3>
                                <p style="margin: 0 0 16px 0; color: #757575; font-size: 14px;">
                                    Your job completed successfully. The following files are available:
                                </p>
                                <ul style="margin: 0; padding-left: 20px; color: #212121; font-size: 14px; line-height: 1.8;">
                                    <li>TRS Signature File</li>
                                    <li>TRO Declaration</li>
                                    <li>Trusted Timestamp</li>
                                    <li>Replicated Package</li>
                                    <li>Run Output Log</li>
                                </ul>
                            </div>

                            <!-- View Results Button -->
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="margin: 24px 0;">
                                <tr>
                                    <td align="center">
                                        <a href="${submission_url}" style="display: inline-block; padding: 12px 32px; background-color: #1976d2; color: #ffffff; text-decoration: none; border-radius: 8px; font-size: 14px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);">
                                            View Results
                                        </a>
                                    </td>
                                </tr>
                            </table>
                            % else:
                            <!-- Error Details -->
                            % if error_message:
                            <div style="margin: 24px 0;">
                                <h3 style="margin: 0 0 16px 0; font-size: 16px; font-weight: 500; color: #212121;">Error Details</h3>
                                <div style="background-color: #ffebee; border-left: 4px solid #f44336; padding: 16px; border-radius: 4px;">
                                    <p style="margin: 0; color: #c62828; font-size: 14px; font-family: 'Courier New', monospace; white-space: pre-wrap; word-break: break-word;">${error_message}</p>
                                </div>
                            </div>
                            % endif

                            <!-- Troubleshooting -->
                            <div style="margin: 24px 0;">
                                <h3 style="margin: 0 0 16px 0; font-size: 16px; font-weight: 500; color: #212121;">Next Steps</h3>
                                <ul style="margin: 0; padding-left: 20px; color: #212121; font-size: 14px; line-height: 1.8;">
                                    <li>Review the error message and job logs</li>
                                    <li>Check your input files and configuration</li>
                                    <li>Consult the <a href="${docs_url}" style="color: #1976d2; text-decoration: none;">user guide</a> for troubleshooting tips</li>
                                    <li>Contact support if you need assistance</li>
                                </ul>
                            </div>

                            <!-- View Details Button -->
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="margin: 24px 0;">
                                <tr>
                                    <td align="center">
                                        <a href="${submission_url}" style="display: inline-block; padding: 12px 32px; background-color: #1976d2; color: #ffffff; text-decoration: none; border-radius: 8px; font-size: 14px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);">
                                            View Details
                                        </a>
                                    </td>
                                </tr>
                            </table>
                            % endif

                            <!-- Additional Info -->
                            <div style="margin-top: 32px; padding-top: 24px; border-top: 1px solid #e0e0e0;">
                                <p style="margin: 0 0 16px 0; color: #757575; font-size: 14px; line-height: 1.6;">
                                    Need help? Check out our <a href="${docs_url}" style="color: #1976d2; text-decoration: none;">documentation</a> or
                                    <a href="mailto:support@sivacor.org" style="color: #1976d2; text-decoration: none;">contact support</a>.
                                </p>
                            </div>
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
