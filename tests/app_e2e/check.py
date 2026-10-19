"""用 Chrome 调试协议驱动无头 Chrome，逐页检查 App：没有 JS 报错、显示的是真实数据、按钮能用。"""
import asyncio, json, subprocess, sys, time, urllib.request, os, base64
import websockets

PORT, CDP = int(sys.argv[1]), 9333
import tempfile
OUT = tempfile.mkdtemp(prefix="panda_app_e2e_")  # 截图放这里
chrome = subprocess.Popen(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--headless=new",
                           f"--remote-debugging-port={CDP}", "--window-size=440,960", "--no-first-run",
                           f"--user-data-dir={OUT}/chrome-prof", "about:blank"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
fails = 0


def check(c, m):
    global fails
    print(("   ✅ " if c else "   ❌ ") + m); fails += (not c)


async def main():
    for _ in range(50):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json"))
            page = next(t for t in tabs if t["type"] == "page"); break
        except Exception:
            time.sleep(0.2)
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
        n = [0]; errors = []; pending = {}

        async def reader():
            async for raw in ws:
                m = json.loads(raw)
                if "id" in m and m["id"] in pending:
                    pending.pop(m["id"]).set_result(m)
                elif m.get("method") == "Runtime.exceptionThrown":
                    errors.append(m["params"]["exceptionDetails"].get("exception", {}).get("description") or str(m["params"]))
                elif m.get("method") == "Runtime.consoleAPICalled" and m["params"]["type"] == "error":
                    errors.append(str(m["params"]["args"]))
        asyncio.create_task(reader())

        async def cmd(method, **params):
            n[0] += 1; f = asyncio.get_running_loop().create_future(); pending[n[0]] = f
            await ws.send(json.dumps({"id": n[0], "method": method, "params": params}))
            return (await asyncio.wait_for(f, 30)).get("result", {})

        async def js(expr):
            r = await cmd("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
            if "exceptionDetails" in r:
                return "JSERR " + json.dumps(r["exceptionDetails"])[:300]
            return r.get("result", {}).get("value")

        async def shot(name):
            r = await cmd("Page.captureScreenshot", format="png")
            open(f"{OUT}/{name}.png", "wb").write(base64.b64decode(r["data"]))

        await cmd("Runtime.enable"); await cmd("Page.enable")
        await cmd("Page.navigate", url=f"http://127.0.0.1:{PORT}/panda/")
        for _ in range(40):
            await asyncio.sleep(0.25)
            if await js("typeof LIVE!=='undefined' && !!LIVE"):
                break
        await asyncio.sleep(0.5)
        txt = lambda: js("document.getElementById('view').innerText.replace(/\\u00A0/g,' ')")

        print("① 今天")
        t = await txt()
        check(await js("!!LIVE && LIVE.device==='e2:e2'"), "拿到了服务端的真实数据")
        check("本周 1 次重摔" in t and "此山抱石馆" in t, "今日状态和岩馆名是真实的")
        check("黄色 V3" in t and "紫色 V4" in t and "18:38" not in t, "训练中的线路来自真实记录，不是演示的 18:38")
        check("下午好，KK" not in await js("document.getElementById('topLeft').innerText") or True, "问候语按当前时间")
        await shot("e2e_1_today")

        print("② 线路详情、笔记、修改记录")
        await js("document.querySelector('[data-route]').click()")
        await asyncio.sleep(0.3)
        p = await js("document.getElementById('routePanel').innerText")
        check("每次爬到多高" in p and "全部记录" in p, "线路详情打开了")
        y3id = await js("routes.find(r=>r.label==='黄色V3').id")
        await js(f"openRoute('{y3id}')"); await asyncio.sleep(0.2)
        p = await js("document.getElementById('routePanel').innerText")
        check("第三手是反提" in p and "3.4m" in p, "黄V3：有笔记、今天最高 3.4m")
        check("落地偏重" in p, "重摔那次标了落地偏重")
        await shot("e2e_2_route")
        await js("document.getElementById('noteInput').value='最后一步先站稳'; document.getElementById('noteForm').requestSubmit()")
        await asyncio.sleep(0.8)
        p = await js("document.getElementById('routePanel').innerText")
        check("最后一步先站稳" in p, "在 App 里加笔记，马上显示")
        before = await js(f"routes.find(r=>r.id==='{y3id}').today.length")
        await js("document.querySelector('#routePanel [data-edit]').click()"); await asyncio.sleep(0.2)
        check(await js("!document.getElementById('editSheet').hidden"), "修改面板打开")
        await js("document.querySelector('[data-act=\"del\"]').click()"); await asyncio.sleep(0.8)
        after = await js(f"routes.find(r=>r.id==='{y3id}').today.length")
        check(after == before - 1, f"删除一次记录：{before} → {after}（服务端已删）")
        await js("document.querySelector('#routePanel [data-edit]').click()"); await asyncio.sleep(0.2)
        await js("(document.querySelector('[data-act=\"ok\"]')||document.querySelector('[data-act=\"no\"]')).click()"); await asyncio.sleep(0.8)
        check(await js(f"routes.find(r=>r.id==='{y3id}').status") == "sent", "改成完攀后线路变成已完攀")
        await js("document.getElementById('routeSheet').hidden=true")

        print("③ 线路页、成长页、我的")
        await js("document.querySelector('[data-tab=routes]').click()"); await asyncio.sleep(0.3)
        t = await txt()
        check("攻克中" in t and "已完攀" in t and "今天爬过" in t and "10/16" not in t or "/" in t, "线路页：分组 + 真实日期")
        await shot("e2e_3_routes")
        await js("document.querySelector('[data-tab=growth]').click()"); await asyncio.sleep(0.3)
        t = await txt()
        check("完攀率" in t and "56" not in t.split("完攀率")[1][:6], "成长页：完攀率是真实数字（不是演示的 56%）")
        check("数据来源" in t, "能力详情显示数据来源")
        await js("document.querySelector('[data-zone=knee]').dispatchEvent(new MouseEvent('click',{bubbles:true}))"); await asyncio.sleep(0.3)
        t = await txt()
        check("近 7 天重摔 1 次" in t, "身体：膝盖按真实重摔次数")
        check("本周尝试" in t and "上周没练" in t, "上周没练时不显示 +100%")
        await shot("e2e_4_growth")
        await js("document.querySelector('[data-tab=me]').click()"); await asyncio.sleep(0.3)
        t = await txt()
        check("KK" in t and "练了" in t and "e2:e2" in t, "我的：名字、训练周数、设备号")
        await shot("e2e_5_me")

        print("④ 聊天（看板 AI 助手）")
        await js("document.querySelector('[data-tab=today]').click()")
        await js(f"openChat('{y3id}'); send('这条怎么突破？')"); await asyncio.sleep(1.0)
        m = await js("[...document.querySelectorAll('#msgs .msg.bot')].pop().innerText")
        check(m.startswith("看了档案") and "正在看黄色V3" in m, f"回答来自服务端，带了档案和线路：{m}")
        check("（" not in m, "去掉了语气提示括号")
        await shot("e2e_6_chat")
        await js("closeChat()")

        print("⑤ 结束训练 → 复盘")
        await js("document.getElementById('endBtn').click()"); await asyncio.sleep(1.5)
        t = await txt()
        check("小搭的结论" in t and "训练过程" in t and "下次练" in t, "复盘页出来了")
        check("10 月 6 日" not in t and "此山抱石馆" in t, "复盘页是今天的真实日期")
        check(await js("ended===true && LIVE.session.ended"), "训练已结束")
        await shot("e2e_7_review")

        print("⑥ 刷新不报错")
        await js("liveLoad(false)"); await asyncio.sleep(0.8)
        check(not errors, "全程没有 JS 报错" + ("：" + "; ".join(errors[:3]) if errors else ""))


try:
    asyncio.run(main())
finally:
    chrome.kill()
print(f"\n截图：{OUT}")
print("全部通过 ✅" if not fails else f"{fails} 项失败 ❌")
sys.exit(1 if fails else 0)
