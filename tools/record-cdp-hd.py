"""HD variant of record-cdp.py: force 1920x1080 viewport, reload, then record.

Usage: python tools/record-cdp-hd.py <cdp_port> <seconds> <out.mp4>
"""

import asyncio
import json
import sys
import base64
import aiohttp

PORT, SECONDS, OUT = sys.argv[1], float(sys.argv[2]), sys.argv[3]


async def main():
    async with aiohttp.ClientSession() as s:
        targets = await (await s.get(f"http://localhost:{PORT}/json")).json()
        page = next(
            t for t in targets if t["type"] == "page" and "chrome" not in t["url"]
        )
        async with s.ws_connect(page["webSocketDebuggerUrl"]) as ws:
            nid = 0

            async def call(method, params=None):
                nonlocal nid
                nid += 1
                await ws.send_json(
                    {"id": nid, "method": method, "params": params or {}}
                )
                async for raw in ws:
                    m = json.loads(raw.data)
                    if m.get("id") == nid:
                        return m

            print(
                "metrics:",
                await call(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": 1920,
                        "height": 1080,
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    },
                ),
            )
            print("reload:", await call("Page.reload"))
            await asyncio.sleep(4)
            print("start:", await call("Page.startScreenRecording"))
            await asyncio.sleep(SECONDS)
            r = await call("Page.stopScreenRecording")
            handle = r.get("result", {}).get("stream")
            data, off = b"", 0
            if handle:
                while True:
                    rr = await call(
                        "IO.read", {"handle": handle, "offset": off, "size": 1 << 22}
                    )
                    res = rr.get("result", {})
                    chunk = res.get("data", "")
                    data += (
                        base64.b64decode(chunk)
                        if res.get("base64Encoded")
                        else chunk.encode()
                    )
                    if res.get("eof"):
                        break
                    off += len(chunk)
                await call("IO.close", {"handle": handle})
            open(OUT, "wb").write(data)
            print("wrote", len(data), "->", OUT)


asyncio.run(main())
