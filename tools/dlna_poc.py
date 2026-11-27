"""POC: a video from the hub played by the TV's own DLNA renderer (UPnP AVTransport), the Station not
involved. Runs on the hub (python3, stdlib only):

    python3 dlna_poc.py info                       # the renderer's formats and state
    python3 dlna_poc.py play <http url> [title]    # SetAVTransportURI + Play
    python3 dlna_poc.py state | pause | resume | stop
    python3 dlna_poc.py seek <hh:mm:ss>
    python3 dlna_poc.py find <words>               # a file on the hub's MiniDLNA -> its url

TV and MiniDLNA addresses come from env TV_DMR (http://<tv>:<port>) and DLNA_SERVER.
2026-11-27: a TP Vision (Philips) TV took an mkv (H.264 + AC3) from MiniDLNA and played it, state and
position read back, Stop worked - the TV decodes, the Station does nothing.
"""
import os
import re
import sys
import urllib.request
from xml.sax.saxutils import escape

TV = os.environ.get("TV_DMR", "")              # the renderer's base, e.g. http://<tv>:2870 (its dmr.xml)
DMS = os.environ.get("DLNA_SERVER", "http://127.0.0.1:8200")


def soap(base, path, service, action, args=""):
    body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">{args}</u:{action}></s:Body></s:Envelope>')
    req = urllib.request.Request(base + path, data=body.encode(), headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}"


def av(action, args=""):
    return soap(TV, "/AVTransport/control", "AVTransport", action, "<InstanceID>0</InstanceID>" + args)


def tags(xml, *names):
    return {n: (re.search(f"<{n}>(.*?)</{n}>", xml, re.S) or [None, None])[1] for n in names}


def didl(url, title, mime):
    item = (f'<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            f'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"><item id="1" parentID="0" restricted="1">'
            f'<dc:title>{escape(title)}</dc:title><upnp:class>object.item.videoItem.movie</upnp:class>'
            f'<res protocolInfo="http-get:*:{mime}:*">{escape(url)}</res></item></DIDL-Lite>')
    return escape(item)


MIME = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".avi": "video/x-msvideo", ".mpg": "video/mpeg",
        ".ts": "video/mp2t", ".m4v": "video/mp4"}


def main(cmd, *a):
    if cmd == "info":
        r = soap(TV, "/ConnectionManager/control", "ConnectionManager", "GetProtocolInfo")
        sink = tags(r, "Sink")["Sink"] or r
        print("video formats:", sorted({x.split(":")[2] for x in sink.split(",") if ":video/" in x}))
        print(tags(av("GetTransportInfo"), "CurrentTransportState", "CurrentTransportStatus"))
    elif cmd == "play":
        url, title = a[0], (a[1] if len(a) > 1 else os.path.basename(a[0]))
        mime = MIME.get(os.path.splitext(url.split("?")[0])[1].lower(), "video/x-matroska")
        print(av("SetAVTransportURI", f"<CurrentURI>{escape(url)}</CurrentURI><CurrentURIMetaData>{didl(url, title, mime)}</CurrentURIMetaData>")[-300:])
        print(av("Play", "<Speed>1</Speed>")[-200:])
    elif cmd == "state":
        print(tags(av("GetTransportInfo"), "CurrentTransportState", "CurrentTransportStatus"))
        print(tags(av("GetPositionInfo"), "TrackDuration", "RelTime", "TrackURI"))
    elif cmd in ("pause", "stop"):
        print(av(cmd.capitalize())[-200:])
    elif cmd == "resume":
        print(av("Play", "<Speed>1</Speed>")[-200:])
    elif cmd == "seek":
        print(av("Seek", f"<Unit>REL_TIME</Unit><Target>{a[0]}</Target>")[-200:])
    elif cmd == "find":
        # MiniDLNA's own search: the item's res url (served by MiniDLNA, range requests work)
        r = soap(DMS, "/ctl/ContentDir", "ContentDirectory", "Search",
                 f"<ContainerID>0</ContainerID><SearchCriteria>dc:title contains &quot;{escape(' '.join(a))}&quot;</SearchCriteria>"
                 "<Filter>*</Filter><StartingIndex>0</StartingIndex><RequestedCount>10</RequestedCount><SortCriteria></SortCriteria>")
        res = tags(r, "Result")["Result"] or ""
        import html
        res = html.unescape(res)
        for t, u in re.findall(r"<dc:title>(.*?)</dc:title>.*?<res[^>]*>(.*?)</res>", res, re.S):
            print(t, "->", u)


if __name__ == "__main__":
    main(*sys.argv[1:])
