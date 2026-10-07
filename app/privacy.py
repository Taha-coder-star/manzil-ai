"""Public privacy policy page, required by Meta to publish the app.

Keep it accurate to what the code actually does. The contact address comes
from PRIVACY_CONTACT_EMAIL so no personal address is committed.
"""

from html import escape

UPDATED = "2026-10-08"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Manzil AI privacy policy</title>
<style>
body {{ font-family: system-ui, sans-serif; line-height: 1.55; color: #1f2328;
       max-width: 46rem; margin: 2rem auto; padding: 0 1rem; }}
h1 {{ font-size: 1.6rem; }} h2 {{ font-size: 1.15rem; margin-top: 1.6rem; }}
.note {{ background: #fff4e5; border-left: 3px solid #e07b00; padding: .6rem .9rem; }}
</style>
</head>
<body>
<h1>Manzil AI privacy policy</h1>
<p>Last updated: {updated}</p>

<p class="note"><strong>Demo phase.</strong> Manzil AI is currently a demonstration used only
by its development team on test phones, with fictional property listings. Please do not send
personal or sensitive information.</p>

<h2>What Manzil AI is</h2>
<p>Manzil AI is a WhatsApp assistant for real-estate agencies. Customers can ask about property
listings by text or voice note, and are put in touch with a human agent of the agency. Agency
staff can manage their listings by WhatsApp. Messages are answered on behalf of the agency whose
WhatsApp number you contacted. You are told when you are talking to an automated assistant.</p>

<h2>Information we process</h2>
<ul>
<li>Your WhatsApp number and WhatsApp profile name.</li>
<li>The messages you send, including text, voice notes and images, and their delivery status.</li>
<li>Your property requirements (for example area, budget and size) and the listings shown to you.</li>
<li>For agency staff: the listing details and photos they submit.</li>
</ul>

<h2>How we use it</h2>
<ul>
<li>To answer your property questions and show matching listings.</li>
<li>To hand your conversation to a human agent of the agency when you ask for one.</li>
<li>To keep the service secure and reliable (for example, preventing duplicate processing).</li>
</ul>
<p>We do not sell your information and do not use it for advertising.</p>

<h2>Service providers</h2>
<p>Messages are delivered through Meta's WhatsApp Business Platform. Our servers and storage run
on Amazon Web Services. To understand voice notes and write replies, message content is processed
by speech-to-text and AI providers (currently ElevenLabs and Google Gemini) under their own terms.
We do not use WhatsApp messages to train our own models.</p>

<h2>Retention</h2>
<p>Demo-phase data is kept only for testing and is deleted when the demo ends, or earlier on
request.</p>

<h2 id="deletion">Deleting your data</h2>
<p>To have your messages and related data deleted, contact us{contact_sentence} and include the
WhatsApp number you used. We will confirm once the data has been deleted.</p>

<h2>Contact</h2>
<p>{contact_line}</p>
</body>
</html>
"""


def render_privacy_page(contact_email):
    if contact_email:
        email = escape(contact_email)
        link = f'<a href="mailto:{email}">{email}</a>'
        contact_sentence, contact_line = f" at {link}", f"Questions about this policy: {link}."
    else:
        contact_sentence = " through the agency you contacted"
        contact_line = "Questions about this policy: please contact the agency you messaged."
    return _PAGE.format(
        updated=UPDATED, contact_sentence=contact_sentence, contact_line=contact_line
    )
