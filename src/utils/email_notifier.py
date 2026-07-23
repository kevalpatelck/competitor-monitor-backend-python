import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from src.utils.logger import logger

def send_changelog_email(changes_list: list):
    """
    Send email notification containing the comparison changelog items.
    """
    if not changes_list:
        logger.info("[EMAIL] No changes to report. Skipping email.")
        return

    # Load SMTP settings from environment
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = os.getenv("SMTP_PORT")
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    receiver_email = os.getenv("RECEIVER_EMAIL")

    if not all([smtp_server, smtp_port, smtp_user, smtp_password, receiver_email]):
        logger.warning(
            "[EMAIL] Email notifications are enabled, but SMTP environment variables are missing. "
            "Please configure SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, and RECEIVER_EMAIL in your .env file."
        )
        return

    try:
        smtp_port = int(smtp_port)
    except ValueError:
        logger.error(f"[EMAIL] Invalid SMTP_PORT: {smtp_port}")
        return

    logger.info(f"[EMAIL] Preparing changelog email to {receiver_email}...")

    # Build HTML Content
    html_content = f"""
    <html>
    <head>
        <style>
            body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #f9fafb; color: #1f2937; margin: 0; padding: 20px; }}
            .container {{ max-width: 600px; background-color: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 24px; margin: 0 auto; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); }}
            h2 {{ color: #1e3a8a; margin-top: 0; border-bottom: 2px solid #3b82f6; padding-bottom: 8px; }}
            p {{ line-height: 1.5; font-size: 14px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; }}
            th {{ background-color: #f3f4f6; color: #4b5563; font-weight: 600; text-align: left; padding: 10px; border-bottom: 1px solid #e5e7eb; }}
            td {{ padding: 10px; border-bottom: 1px solid #f3f4f6; vertical-align: top; }}
            .badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold; text-transform: uppercase; }}
            .badge-price {{ background-color: #d1fae5; color: #065f46; }}
            .badge-sku {{ background-color: #dbeafe; color: #1e40af; }}
            .badge-messaging {{ background-color: #fef3c7; color: #92400e; }}
            .badge-product {{ background-color: #f3e8ff; color: #6b21a8; }}
            .footer {{ margin-top: 24px; font-size: 11px; color: #9ca3af; border-top: 1px solid #e5e7eb; padding-top: 12px; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>🔔 Competitor Monitor Change Notification</h2>
            <p>We detected updates on your tracked competitor websites. Below is a summary of the differences found:</p>
            <table>
                <thead>
                    <tr>
                        <th style="width: 25%;">Competitor / Page</th>
                        <th style="width: 20%;">Change Type</th>
                        <th style="width: 55%;">Detail Summary</th>
                    </tr>
                </thead>
                <tbody>
    """

    for change in changes_list:
        badge_class = "badge-product" if change.get("change_type") == "product_price" else \
                      "badge-price" if change.get("change_type") == "price" else \
                      "badge-sku" if change.get("change_type") == "sku" else \
                      "badge-messaging"

        change_label = {
            "price": "Avg Price",
            "sku": "SKU Count",
            "messaging": "Messaging",
            "product_price": "Product Price"
        }.get(change.get("change_type"), change.get("change_type"))

        html_content += f"""
                    <tr>
                        <td>
                            <strong>{change.get('site_name')}</strong><br/>
                            <span style="color: #6b7280; font-size: 11px;">{change.get('page_label')}</span>
                        </td>
                        <td>
                            <span class="badge {badge_class}">{change_label}</span>
                        </td>
                        <td>
                            {change.get('summary')}
                        </td>
                    </tr>
        """

    html_content += """
                </tbody>
            </table>
            <p style="margin-top: 20px;">For full details and to view the product lists, please open your <a href="http://localhost:5173" target="_blank" style="color: #3b82f6; text-decoration: none; font-weight: bold;">Competitor Monitor Dashboard</a>.</p>
            <div class="footer">
                This is an automated notification from your Competitor Monitoring System.
            </div>
        </div>
    </body>
    </html>
    """

    # Assemble MIME Message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔔 Competitor Monitor: {len(changes_list)} Change(s) Detected"
    msg["From"] = smtp_user
    msg["To"] = receiver_email

    msg.attach(MIMEText(html_content, "html"))

    try:
        # Connect using SSL or TLS with a strict 10-second timeout to prevent hanging the scan pipeline
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
            server.starttls()

        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, receiver_email, msg.as_string())
        server.quit()
        logger.info("[EMAIL] Change notification email sent successfully!")
    except Exception as e:
        logger.error(f"[EMAIL] Failed to send email: {e}")
