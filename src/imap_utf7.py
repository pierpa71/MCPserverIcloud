"""Codifica/decodifica dei nomi cartella IMAP (Modified UTF-7, RFC 3501 §5.1.3)."""

import base64


def encode(name: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(b).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def decode(name: str) -> str:
    out, i = [], 0
    while i < len(name):
        ch = name[i]
        if ch == "&":
            j = name.index("-", i)
            chunk = name[i + 1 : j]
            if not chunk:
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)
