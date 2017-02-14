# These are methods to do with MIME and crypto, implementing PGP/MIME.

import re
import StringIO
import email.parser

from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase

from mailpile.crypto.state import EncryptionInfo, SignatureInfo
from mailpile.i18n import gettext as _
from mailpile.i18n import ngettext as _n
from mailpile.mail_generator import Generator


##[ Common utilities ]#########################################################

def Normalize(payload):
    # http://tools.ietf.org/html/rfc3156 says we must:
    #
    #   - use CRLF everywhere
    #   - strip trailing whitespace
    #   - end with a CRLF
    #
    # In particlar, the stripping of trailing whitespace seems (based on
    # experiments with mutt), to in practice mean "strip trailing whitespace
    # off the last line"...
    #
    text = re.sub(r'\r?\n', '\r\n', payload).rstrip(' \t')
    if not text.endswith('\r\n'):
        text += '\r\n'
    return text


def MessageAsString(part, unixfrom=False):
    buf = StringIO.StringIO()
    Generator(buf).flatten(part, unixfrom=unixfrom)
    return Normalize(buf.getvalue()).replace('--\r\n--', '--\r\n\r\n--')


class EncryptionFailureError(ValueError):
    def __init__(self, message, to_keys):
        ValueError.__init__(self, message)
        self.to_keys = to_keys


class SignatureFailureError(ValueError):
    def __init__(self, message, from_key):
        ValueError.__init__(self, message)
        self.from_key = from_key


DEFAULT_CHARSETS = ['utf-8', 'iso-8859-1']


def _decode_text_part(part, payload, charsets=None):
    for cs in (c for c in ([part.get_content_charset() or None] +
                           (charsets or DEFAULT_CHARSETS)) if c):
        try:
            return cs, payload.decode(cs)
        except (UnicodeDecodeError, TypeError, LookupError):
            pass
    return '8bit', payload


def _update_text_payload(part, payload, charsets=None):
    if 'content-transfer-encoding' in part:
        # We want this recalculated by the charset setting below
        del part['content-transfer-encoding']
    charset, payload = _decode_text_part(part, payload, charsets=charsets)
    part.set_payload(payload, charset)


##[ Methods for unwrapping encrypted parts ]###################################


def MimeReplaceFilename(header, filename):
    """
    Accepts a MIME header containing filename="...".
    Replaces it with filename.
    """
    start = header.find('filename=')
    start = header.find('"', start)
    end = header.find('"', start+1)+1

    if start > 0 and end > start:
        headernew = header[:start+1] + filename + header[end-1:]
    else:
        headernew = header[:]

    return headernew


def MimeTrimFilename(header, extension):
    """
    Accepts a MIME header containing filename="...".
    If the name ends with '.' + extension that ending is trimmed off.
    """
    start = header.find('filename=')
    start = header.find('"', start)
    end = header.find('"', start+1)+1
    start = header.find('.'+extension+'"', start, end)

    if start > 0 and end > start:
        headernew = header[:start] + header[end-1:]
    else:
        headernew = header[:]

    return headernew


def MimeReplacePart(part, newpart, keep_old_headers=False):
    """
    Replace a MIME part with new version (decrypted, signature verified, ... ).
    retaining headers from the old part that are not in the new part. The
    headers that would be overwritten will be renamed and kept if the
    keep_old_headers variable is set to a prefix string.

    MIME headers (Content-*) get special treatment.

    Returns a set of the headers that got copied from the new part.
    """
    part.set_payload(newpart.get_payload())

    # Original MIME headers must go, whether we're replacing them or not.
    for hdr in [k for k in part.keys() if k.lower().startswith('content-')]:
        del part[hdr]

    # If we're keeping the non-MIME old headers, make copies now before
    # they get deleted below.
    if keep_old_headers:
        if not isinstance(keep_old_headers, str):
            keep_old_headers = "Old"
        for h in newpart.keys():
            headers = (part.get_all(h) or [])
            if (len(headers) == 1) and (part[h] == newpart[h]):
                continue
            for v in headers:
                part.add_header('X-%s-%s' % (keep_old_headers, h), v)

    for h in newpart.keys():
        del part[h]

    copied = set([])
    for h, v in newpart.items():
        part.add_header(h, v)
        copied.add(h)

    return copied


def UnwrapMimeCrypto(part, protocols=None, psi=None, pei=None, charsets=None,
                     depth=0):
    """
    This method will replace encrypted and signed parts with their
    contents and set part attributes describing the security properties
    instead.
    """

    # Guard against maliciously constructed emails
    if depth > 6:
        return

    part.signature_info = SignatureInfo(parent=psi)
    part.encryption_info = EncryptionInfo(parent=pei)

    part.signed_headers = set([])
    part.encrypted_headers = set([])

    mimetype = part.get_content_type() or 'text/plain'
    disposition = part['content-disposition'] or ""
    encoding = part['content-transfer-encoding'] or ""

    # FIXME: Check the protocol. PGP? Something else?
    # FIXME: This is where we add hooks for other MIME encryption
    #        schemes, so route to callbacks by protocol.
    crypto_cls = protocols['openpgp']

    if part.is_multipart():
        # Containers are by default not bubbly
        part.signature_info.bubbly = False
        part.encryption_info.bubbly = False

    if part.is_multipart() and mimetype == 'multipart/signed':
        try:
            boundary = part.get_boundary()
            payload, signature = part.get_payload()

            # The Python get_payload() method likes to rewrite headers,
            # which breaks signature verification. So we manually parse
            # out the raw payload here.
            head, raw_payload, junk = part.as_string(
                ).replace('\r\n', '\n').split('\n--%s\n' % boundary, 2)

            part.signature_info = crypto_cls().verify(
                Normalize(raw_payload), signature.get_payload())
            part.signature_info.bubble_up(psi)

            # Reparent the contents up, removing the signature wrapper
            hdrs = MimeReplacePart(part, payload,
                                   keep_old_headers='MH-Renamed')
            part.signed_headers = hdrs

            # Try again, in case we just unwrapped another layer
            # of multipart/something.
            UnwrapMimeCrypto(part,
                             protocols=protocols,
                             psi=part.signature_info,
                             pei=part.encryption_info,
                             charsets=charsets,
                             depth = depth + 1 )

        except (IOError, OSError, ValueError, IndexError, KeyError):
            part.signature_info = SignatureInfo()
            part.signature_info["status"] = "error"
            part.signature_info.bubble_up(psi)

    elif part.is_multipart() and mimetype == 'multipart/encrypted':
        try:
            preamble, payload = part.get_payload()

            (part.signature_info, part.encryption_info, decrypted
             ) = crypto_cls().decrypt(payload.as_string())
        except (IOError, OSError, ValueError, IndexError, KeyError):
            part.encryption_info = EncryptionInfo()
            part.encryption_info["status"] = "error"

        part.signature_info.bubble_up(psi)
        part.encryption_info.bubble_up(pei)

        if part.encryption_info['status'] == 'decrypted':
            newpart = email.parser.Parser().parse(
                StringIO.StringIO(decrypted))

            # Reparent the contents up, removing the encryption wrapper
            hdrs = MimeReplacePart(part, newpart,
                                   keep_old_headers='MH-Renamed')

            # FIXME: If there is a Memory-Hole force-display part, we
            #        should suppress it since we handle MH natively.

            part.encrypted_headers = hdrs
            if part.signature_info["status"] != 'none':
                part.signed_headers = hdrs

            # Try again, in case we just unwrapped another layer
            # of multipart/something.
            UnwrapMimeCrypto(part,
                             protocols=protocols,
                             psi=part.signature_info,
                             pei=part.encryption_info,
                             charsets=charsets,
                             depth = depth + 1 )

    # If we are still multipart after the above shenanigans (perhaps due
    # to an error state), recurse into our subparts and unwrap them too.
    elif part.is_multipart():
        for sp in part.get_payload():
            UnwrapMimeCrypto(sp,
                             protocols=protocols,
                             psi=part.signature_info,
                             pei=part.encryption_info,
                             charsets=charsets,
                             depth = depth + 1 )

    elif disposition.startswith('attachment'):
        # The sender can attach signed/encrypted/key files without following
        # rules for naming or mime type.
        # So - sniff to detect parts that need processing and identify protocol.
        for protocol in protocols:
            crypto_cls = protocols[protocol]
            kind = crypto_cls().sniff(part.get_payload(), encoding)
            if kind:
                break

        if 'encrypted' in kind or 'signature' in kind:
            # Messy! The PGP decrypt operation is also needed for files which
            # are encrypted and signed, and files that are signed only.
            payload = part.get_payload( None, True )
            try:
                (part.signature_info, part.encryption_info, decrypted
                 ) = crypto_cls().decrypt(payload)
            except (IOError, OSError, ValueError, IndexError, KeyError):
                part.encryption_info = EncryptionInfo()
                part.encryption_info["status"] = "error"

            part.signature_info.bubble_up(psi)
            part.encryption_info.bubble_up(pei)

            if (part.encryption_info['status'] == 'decrypted' or
                    part.signature_info['status'] == 'verified'):
                newpart = email.parser.Parser().parse(
                    StringIO.StringIO(decrypted))

                # Use the original file name if available, otherwise
                # delete .gpg or .asc extension from attachment file name.
                if part.encryption_info.filename:
                    disposition = MimeReplaceFilename(disposition,
                                        part.encryption_info.filename)
                elif 'armored' in kind:
                    disposition = MimeTrimFilename(disposition, 'asc')
                else:
                    disposition = MimeTrimFilename(disposition, 'gpg')
                newpart.add_header('content-disposition', disposition)

                MimeReplacePart(part, newpart)

                # Is there another layer to unwrap?
                UnwrapMimeCrypto(part,
                                 protocols=protocols,
                                 psi=part.signature_info,
                                 pei=part.encryption_info,
                                 charsets=charsets,
                                 depth = depth + 1 )
            else:
                # FIXME: Best action for unsuccessful attachment processing?
                pass

    elif mimetype == 'text/plain':
        return UnwrapPlainTextCrypto(part,
                                     protocols=protocols,
                                     psi=psi,
                                     pei=pei,
                                     charsets=charsets,
                                     depth = depth + 1 )

    else:
        # FIXME: This is where we would handle cryptoschemes that don't
        #        appear as multipart/...
        pass


    # Mix in our bubbles
    part.signature_info.mix_bubbles()
    part.encryption_info.mix_bubbles()

    # Bubble up!
    part.signature_info.bubble_up(psi)
    part.encryption_info.bubble_up(pei)


def UnwrapPlainTextCrypto(part, protocols=None, psi=None, pei=None,
                                charsets=None, depth = 0 ):
    """
    This method will replace encrypted and signed parts with their
    contents and set part attributes describing the security properties
    instead.
    """
    payload = part.get_payload(None, True).strip()
    si = SignatureInfo(parent=psi)
    ei = EncryptionInfo(parent=pei)
    for crypto_cls in protocols.values():
        crypto = crypto_cls()

        if (payload.startswith(crypto.ARMOR_BEGIN_ENCRYPTED) and
                payload.endswith(crypto.ARMOR_END_ENCRYPTED)):
            try:
                si, ei, text = crypto.decrypt(payload)
                _update_text_payload(part, text, charsets=charsets)
            except (IOError, OSError, ValueError, IndexError, KeyError):
                ei = EncryptionInfo()
                ei["status"] = "error"
            break

        elif (payload.startswith(crypto.ARMOR_BEGIN_SIGNED) and
                payload.endswith(crypto.ARMOR_END_SIGNED)):
            try:
                si = crypto.verify(payload)
            except (IOError, OSError, ValueError, IndexError, KeyError):
                si = SignatureInfo()
                si["status"] = "error"
            _update_text_payload(part, crypto.remove_armor(payload),
                                 charsets=charsets)
            break

    part.signature_info = si
    part.signature_info.bubble_up(psi)
    part.encryption_info = ei
    part.encryption_info.bubble_up(pei)


##[ Methods for stripping down message headers ]###############################


def ObscureSubject(subject):
    """
    Replace the Subject line with something nondescript.
    """
    return _("Encrypted Message")


def ObscureNames(hdr):
    """
    Remove names (leaving e-mail addresses) from the To: and Cc: headers.

    >>> ObscureNames("Bjarni R. E. <bre@klaki.net>, e@b.c (Elmer Boop)")
    u'<bre@klaki.net>, <e@b.c>'

    """
    from mailpile.mailutils import AddressHeaderParser
    return ', '.join('<%s>' % ai.address for ai in AddressHeaderParser(hdr))


def ObscureSender(sender):
    """
    Remove as much metadata from the From: line as possible.
    """
    return ObscureNames(sender)


def ObscureAllRecipients(sender):
    """
    Remove all content from the To: and Cc: lines entirely.
    """
    return "recipients-suppressed;"


# A dictionary for use with MimeWrapper's obscured_headers parameter,
# that will obscure as much of the metadata from the public header as
# possible without breaking compatibility.
OBSCURE_HEADERS_MILD = {
    'subject': ObscureSubject,
    'from': ObscureSender,
    'sender': ObscureSender,
    'reply-to': ObscureSender,
    'to': ObscureNames,
    'cc': ObscureNames,
    'user-agent': lambda t: None}


# A dictionary for use with MimeWrapper's obscured_headers parameter,
# that will obscure as much of the metadata from the public header as
# possible. This is only useful with encrypted messages and will badly
# break things unless the recipient is running an MUA that fully implements
# Memory Hole.
OBSCURE_HEADERS_EXTREME = {
    'subject': ObscureSubject,
    'from': ObscureSender,
    'sender': ObscureSender,
    'reply-to': ObscureSender,
    'to': ObscureAllRecipients,
    'cc': lambda t: None,
    'date': lambda t: None,
    'in-reply-to': lambda t: None,
    'references': lambda t: None,
    'openpgp': lambda t: None,
    'user-agent': lambda t: None}


##[ Methods for encrypting and signing ]#######################################

class MimeWrapper:
    CONTAINER_TYPE = 'multipart/mixed'
    CONTAINER_PARAMS = ()

    # These are the default "memory hole" settings; wrap/protect the
    # important user-visible headers.
    WRAPPED_HEADERS = ('subject', 'from', 'to', 'cc', 'date', 'user-agent',
                       'sender', 'reply-to', 'in-reply-to', 'references',
                       'openpgp', 'autocrypt')

    # Force-displayed headers; if these headers get obscured, add a
    # visible part that shows them to the user in legacy clients.
    FORCE_DISPLAY_HEADERS = ('subject', 'from', 'to', 'cc')

    # By default, no headers are obscured. That's a user preference,
    # since there's a trade-off between privacy and compatibility.
    OBSCURED_HEADERS = {}

    def __init__(self, config,
                 event=None, cleaner=None,
                 sender=None, recipients=None,
                 use_html_wrapper=False,
                 wrapped_headers=None,
                 obscured_headers=None):
        from mailpile.mailutils import MakeBoundary
        self.config = config
        self.event = event
        self.sender = sender
        self.cleaner = cleaner
        self.recipients = recipients or []
        self.use_html_wrapper = use_html_wrapper
        self.container = c = MIMEMultipart(boundary=MakeBoundary())

        self.wrapped_headers = self.WRAPPED_HEADERS
        if wrapped_headers is not None:
            self.wrapped_headers = wrapped_headers or ()

        self.obscured_headers = self.OBSCURED_HEADERS
        if obscured_headers is not None:
            self.obscured_headers = obscured_headers or {}

        c.set_type(self.CONTAINER_TYPE)
        c.signature_info = SignatureInfo(bubbly=False)
        c.encryption_info = EncryptionInfo(bubbly=False)
        if self.cleaner:
            self.cleaner(self.container)
        for pn, pv in self.CONTAINER_PARAMS:
            self.container.set_param(pn, pv)

    def crypto(self):
        return NotImplementedError("Please override me")

    def attach(self, part):
        c = self.container
        c.attach(part)

        if not hasattr(part, 'signature_info'):
            part.signature_info = SignatureInfo(parent=c.signature_info)
            part.encryption_info = EncryptionInfo(parent=c.encryption_info)
        else:
            part.signature_info.parent = c.signature_info
            part.signature_info.bubbly = True
            part.encryption_info.parent = c.encryption_info
            part.encryption_info.bubbly = True

        if self.cleaner:
            self.cleaner(part)
        del part['MIME-Version']
        return self

    def get_keys(self, people):
        return people

    def flatten(self, msg, unixfrom=False):
        return MessageAsString(msg, unixfrom=unixfrom)

    def get_only_text_part(self, msg):
        count = 0
        only_text_part = None
        for part in msg.walk():
            if part.is_multipart():
                continue
            count += 1
            mimetype = part.get_content_type() or 'text/plain'
            if mimetype != 'text/plain' or count != 1:
                return False
            else:
                only_text_part = part
        return only_text_part

    def wrap(self, msg):
        obscured = self.obscured_headers
        wrapped = self.wrapped_headers

        for h in msg.keys():
            hl = h.lower()

            # FIXME: Pretty sure this doesn't do the right thing if there
            #        are multiple headers with the same name.

            if not hl.startswith('content-') and not hl.startswith('mime-'):
                if hl in obscured:
                    oh = obscured[hl](msg[h])
                    if oh:
                        self.container[h] = oh
                else:
                    self.container[h] = msg[h]
                if hl not in wrapped and hl not in obscured:
                    del msg[h]
            elif hl == 'mime-version':
                del msg[h]

        if hasattr(msg, 'signature_info'):
            self.container.signature_info = msg.signature_info
            self.container.encryption_info = msg.encryption_info

        # FIXME: If any headers from the FORCE_DISPLAY_HEADERS list got
        #        obscured, we should convert to multipart/mixed and add
        #        a force-display part to show them to legacy users.

        return self.container


class MimeSigningWrapper(MimeWrapper):
    CONTAINER_TYPE = 'multipart/signed'
    CONTAINER_PARAMS = ()
    SIGNATURE_TYPE = 'application/x-signature'
    SIGNATURE_DESC = 'Abstract Digital Signature'

    def __init__(self, *args, **kwargs):
        MimeWrapper.__init__(self, *args, **kwargs)

        name = 'signature.html' if self.use_html_wrapper else 'signature.asc'
        self.sigblock = MIMEBase(*self.SIGNATURE_TYPE.split('/'))
        self.sigblock.set_param("name", name)
        for h, v in (("Content-Description", self.SIGNATURE_DESC),
                     ("Content-Disposition",
                      "attachment; filename=\"%s\"" % name)):
            self.sigblock.add_header(h, v)

    def _wrap_sig_in_html(self, sig):
        return (
            "<html><body><h1>%(title)s</h1><p>\n\n%(description)s\n\n</p>"
            "<pre>\n%(sig)s\n</pre><hr>"
            "<i><a href='%(ad_url)s'>%(ad)s</a>.</i></body></html>"
            ) % self._wrap_sig_in_html_vars(sig)

    def _wrap_sig_in_html_vars(self, sig):
        return {
            "title": _("Digital Signature"),
            "description": _(
                "This is a digital signature, which can be used to verify\n"
                "the authenticity of this message. You can safely discard\n"
                "or ignore this file if your e-mail software does not\n"
                "support digital signatures."),
            "ad": _("Generated by Mailpile"),
            "ad_url": _("https://www.mailpile.is/"),  # FIXME: Link to help?
            "sig": sig}

    def _update_crypto_status(self, part):
        part.signature_info.part_status = 'verified'

    def wrap(self, msg, prefer_inline=False):
        from_key = self.get_keys([self.sender])[0]

        if prefer_inline:
            prefer_inline = self.get_only_text_part(msg)
        else:
            prefer_inline = False

        if prefer_inline is not False:
            message_text = Normalize(prefer_inline.get_payload(None, True)
                                     .strip() + '\r\n\r\n')
            status, sig = self.crypto().sign(message_text,
                                             fromkey=from_key,
                                             clearsign=True,
                                             armor=True)
            if status == 0:
                _update_text_payload(prefer_inline, sig)
                self._update_crypto_status(prefer_inline)
                return msg

        else:
            MimeWrapper.wrap(self, msg)
            self.attach(msg)
            self.attach(self.sigblock)
            message_text = self.flatten(msg)
            status, sig = self.crypto().sign(message_text,
                                             fromkey=from_key, armor=True)
            if status == 0:
                if self.use_html_wrapper:
                    sig = self._wrap_sig_in_html(sig)
                self.sigblock.set_payload(sig)
                self._update_crypto_status(self.container)
                return self.container

        raise SignatureFailureError(_('Failed to sign message!'), from_key)


class MimeEncryptingWrapper(MimeWrapper):
    CONTAINER_TYPE = 'multipart/encrypted'
    CONTAINER_PARAMS = ()
    ENCRYPTION_TYPE = 'application/x-encrypted'
    ENCRYPTION_VERSION = 0

    def __init__(self, *args, **kwargs):
        MimeWrapper.__init__(self, *args, **kwargs)

        self.version = MIMEBase(*self.ENCRYPTION_TYPE.split('/'))
        self.version.set_payload('Version: %s\n' % self.ENCRYPTION_VERSION)
        for h, v in (("Content-Disposition", "attachment"), ):
            self.version.add_header(h, v)

        self.enc_data = MIMEBase('application', 'octet-stream')
        for h, v in (("Content-Disposition",
                      "attachment; filename=\"msg.asc\""), ):
            self.enc_data.add_header(h, v)

        self.attach(self.version)
        self.attach(self.enc_data)

    def _encrypt(self, message_text, tokeys=None, armor=False):
        return self.crypto().encrypt(message_text,
                                     tokeys=tokeys, armor=True)

    def _update_crypto_status(self, part):
        part.encryption_info.part_status = 'decrypted'

    def wrap(self, msg, prefer_inline=False):
        to_keys = set(self.get_keys(self.recipients + [self.sender]))

        if prefer_inline:
            prefer_inline = self.get_only_text_part(msg)
        else:
            prefer_inline = False

        if prefer_inline is not False:
            message_text = Normalize(prefer_inline.get_payload(None, True))
            status, enc = self._encrypt(message_text,
                                        tokeys=to_keys,
                                        armor=True)
            if status == 0:
                _update_text_payload(prefer_inline, enc)
                self._update_crypto_status(prefer_inline)
                return msg

        else:
            MimeWrapper.wrap(self, msg)
            del msg['MIME-Version']
            if self.cleaner:
                self.cleaner(msg)
            message_text = self.flatten(msg)
            status, enc = self._encrypt(message_text,
                                        tokeys=to_keys,
                                        armor=True)
            if status == 0:
                self.enc_data.set_payload(enc)
                self._update_crypto_status(self.enc_data)
                return self.container

        raise EncryptionFailureError(_('Failed to encrypt message!'), to_keys)


if __name__ == "__main__":
    import sys
    import doctest

    # FIXME: Add tests for the wrapping/unwrapping code. It's crazy that
    #        we don't have such tests. :-(

    results = doctest.testmod(optionflags=doctest.ELLIPSIS)
    print '%s' % (results, )
    if results.failed:
        sys.exit(1)
