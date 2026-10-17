# 奢侈品官网上新监控

每 10 分钟检查一遍 20 个奢侈品牌的美国官网，一出新品就推到你手机的 Telegram：**图片、品名、价格、有货/缺货码数、"你的码有没有货"、季节（能识别时）、官网链接**。

```
🆕 Khaite · Coats · 女装
The Arlo Wool Coat
💰 $3,450
📏 有货：XS · M · L
　 缺货：S
✅ 你的码有货：M
🏷 FW26
→ 去官网看
```

怎么判断"上新"：程序记住每个品牌官网现在有哪些商品，之后只要出现**之前没见过的商品**就推送。所以**第一次运行只做记录、不推送**，从第二轮开始才会推。

---

## 1. 安装（5 分钟）

需要 Python 3.9 以上。终端（Windows 用"命令提示符"）进到这个文件夹：

```bash
pip install -r requirements.txt
```

> 建议把文件夹放在用户主目录下（如 `~/luxury-monitor`），**不要放在桌面、文稿、下载里**——Mac 对这几个文件夹有权限限制，开机自启时会读不到。

## 2. 建 Telegram bot（3 分钟）

1. 手机 Telegram 里搜 **@BotFather** → 发 `/newbot` → 起个名字 → 它会给你一串 token（形如 `123456:ABC-xyz`）
2. 把 token 填进 `config.yaml` 的 `telegram.bot_token`
3. 在 Telegram 里打开你刚建的 bot，随便发一句话
4. 运行 `python monitor.py chat-id`，把打印出的数字填进 `telegram.chat_id`
5. 运行 `python monitor.py test-notify`，手机收到消息就通了

不想用 Telegram 也可以先不填，上新会打印在终端窗口里。

## 3. 体检（第一次必做）

```bash
python monitor.py probe
```

逐个品牌检查能不能抓、用哪种抓法，最后给一张表：

- ✅ **Shopify**：最理想，直接读官网商品数据，码数库存都准
- ✅ **网页**：读 New In 页 + sitemap 找新链接，再读商品页
- ⚠️ / ❌：按提示处理，常见三种情况：
  - **sitemap 过滤后 0 个链接** → `url_include` 写的美国站路径不对。看 probe 打印的链接长什么样（比如是 `/us/en/` 还是 `/en-us/`），改 `config.yaml` 里那个品牌的 `url_include`
  - **被反爬拦截** → 装浏览器模式：`pip install playwright && playwright install chromium`，再跑 `python monitor.py probe "Gucci" --browser`；能过的话在配置里给它加一行 `browser: true`
  - **网址打不开 / 域名不对** → 改 `url`，或者给那个品牌加 `enabled: false` 先跳过

改完只测一个品牌：`python monitor.py probe "Saint Laurent"`

## 4. 看看推送效果

```bash
python monitor.py demo Khaite
```

会把这个品牌当前最新的 2 件推给你（只是演示，不影响监控记录）。

## 5. 设置你的偏好（`config.yaml` → `settings`）

| 设置 | 作用 |
|---|---|
| `gender` | `all` / `men` / `women` |
| `my_sizes` | 你的码，如 `["M", "48", "42", "US 9"]`，推送里会标"你的码有货" |
| `only_my_sizes` | `true` = 只推你的码有货的 |
| `include_keywords` | 只要某些品类，文件里有"只要外套和鞋"的现成例子，取消注释即可 |
| `exclude_keywords` | 不要的品类，默认已排除包、首饰、香水、围巾帽子等 |
| `interval_minutes` | 检查间隔，默认 10 分钟，别低于 5 |

## 6. 常驻运行

先手动跑一下确认没问题：`python monitor.py run`（Ctrl+C 停止）。然后设置开机自启：

**Mac**
```bash
bash install_mac.sh
```
登录后自动运行、崩溃自动重启、运行期间电脑不会自动睡眠。停止：脚本最后会打印取消命令。

**Windows**
1. 双击 `start_monitor.bat` 试运行
2. 按 `Win+R` 输入 `shell:startup` 回车，把 `start_monitor.bat` 的**快捷方式**拖进去 → 开机自动运行
3. 设置 → 电源 → "睡眠"改成"从不"

**Linux**：`nohup python3 monitor.py run &`，或写成 systemd 服务。

运行日志在 `monitor.log`。

## 7. 日常

- `python monitor.py recent 30`：最近 30 条上新和**首次发现时间**。跑一两周后，这个时间就能拿去和 SSENSE、Farfetch 上同款的上架时间对比，验证"官网更快"
- 某品牌连续失败 6 轮，会推一条 ⚠️ 提醒；恢复后推 ✅
- 断网或 Telegram 没发出去的上新，24 小时内会自动补发
- 某品牌一轮上新超过 15 件（换季大上新），改发一条带链接的清单，避免刷屏
- 改了 `config.yaml` 要重启（Mac：`launchctl unload ~/Library/LaunchAgents/com.luxmonitor.plist` 再 `load`；Windows：关掉窗口再开）

## 关于速度和品牌

- **Shopify 品牌**（Khaite、Totême 等）：商品一发布就能读到，延迟 ≈ 检查间隔（10 分钟）
- **Kering 系等大牌**（Saint Laurent、Balenciaga、Bottega、McQueen…）：New In 页能读到的话也是 10 分钟级；只能靠 sitemap 的话，取决于品牌多久更新一次 sitemap（可能几小时）。跑几天后看 `recent` 就知道每个品牌的实际表现
- LV、Dior、Celine、Hermès 反爬最重，没放进默认清单；Chanel 成衣不在线上卖
- 加品牌：在 `brands` 里加 `name` + `url` 两行，跑一下 probe 即可

## 注意

请保持合理频率（默认 10 分钟一轮、每个网站请求间隔 1.5 秒），个人使用。IP 被某个网站临时限制时，调大 `interval_minutes` 或 `request_delay_seconds`。
