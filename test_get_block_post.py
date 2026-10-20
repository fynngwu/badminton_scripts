"""本地模拟：GET 永不返回时，POST 能否到达服务端。

运行：python test_get_block_post.py
依赖：pip install httpx aiohttp
不访问真实预约服务器，也不提交真实订单。
"""
import asyncio
import contextlib
import logging
import time
from datetime import datetime

import httpx
from aiohttp import web

logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger('test')
T0 = time.perf_counter()


def say(message):
    log.info('[%s +%7.1fms] %s', datetime.now().strftime('%H:%M:%S.%f')[:-3],
             (time.perf_counter() - T0) * 1000, message)


async def get_handler(request):
    say('SERVER ← 收到 GET /getOrderTimeConfigList；故意永远不响应')
    await asyncio.Event().wait()


async def post_handler(request):
    body = await request.json()
    say(f'SERVER ← 真正收到 POST /saveOrder，body={body}')
    return web.json_response({'code': 200, 'success': True, 'msg': '本地模拟成功'})


async def run_case(base, label, max_connections, separate_post_client):
    say(f'\n===== {label} =====')
    limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
    async with httpx.AsyncClient(limits=limits, trust_env=False) as get_client:
        if separate_post_client:
            post_client = httpx.AsyncClient(trust_env=False)
        else:
            post_client = get_client
        try:
            async def do_get(i):
                say(f'CLIENT → GET #{i} 准备发送')
                try:
                    # GET 故意等待超过 POST 的观察窗口，不主动取消
                    await get_client.get(f'{base}/getOrderTimeConfigList', timeout=30.0)
                except asyncio.CancelledError:
                    say(f'CLIENT GET #{i} 在测试结束时取消')
                    raise
                except Exception as e:
                    say(f'CLIENT GET #{i} 异常 {type(e).__name__}: {e!r}')

            tasks = [asyncio.create_task(do_get(i)) for i in range(1, 4)]
            await asyncio.sleep(0.4)
            say('CLIENT → POST 准备调用 httpx（不等 GET 结束）')
            try:
                response = await post_client.post(
                    f'{base}/saveOrder', json={'court': 9, 'demo': True},
                    timeout=httpx.Timeout(6.0, pool=2.0),
                )
                say(f'CLIENT ← POST HTTP {response.status_code} {response.text}')
            except Exception as e:
                say(f'CLIENT POST 异常 {type(e).__name__}: {e!r}')
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if separate_post_client:
                await post_client.aclose()


async def main():
    app = web.Application()
    app.router.add_get('/getOrderTimeConfigList', get_handler)
    app.router.add_post('/saveOrder', post_handler)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    say(f'本地模拟服务器启动：{base}')
    try:
        await run_case(base, 'A: 和原代码一样，共享客户端，默认容量100', 100, False)
        await run_case(base, 'B: 人为限制连接池只有1个连接', 1, False)
        await run_case(base, 'C: 连接池只有1个，但POST使用独立客户端', 1, True)
    finally:
        # GET handler 故意不返回；关闭服务端时取消残留 handler
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
