# email_utils.py
import win32com.client

def sendEmailTo(to_address: str, title: str, body: str) -> bool:
    """
    Sends an email via Outlook from etdp@pilgrimaero.com.

    Args:
        to_address: recipient email address (e.g. "person@pilgrimaero.com")
        title:      subject line
        body:       plain-text body of the message

    Returns:
        True on success, False on failure.
    """
    try:
        # Launch Outlook (or hook into existing instance)
        outlook = win32com.client.Dispatch('Outlook.Application')
        mail    = outlook.CreateItem(0)  # 0 = olMailItem

        # Set mail fields
        mail.To                    = to_address
        mail.Subject               = title
        mail.Body                  = body
        mail.SentOnBehalfOfName    = 'etdp@pilgrimaero.com'

        # Send it!
        mail.Send()
        return True

    except Exception as e:
        # In Fusion add-ins you might log to your text file or the Fusion console:
        print(f"[sendEmailTo] Failed to send email to {to_address}: {e}")
        return False
