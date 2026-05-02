# Hostex 阿佐谷清扫提醒 Bot

这个版本默认监控：

- 阿佐谷A
- 阿佐谷B
- 阿佐谷C

脚本会登录 Hostex 日历，分别抓取三个房源的下一次退房清扫日期。只要三个房源都符合提醒条件，就会同时向企业微信 / WeCom 群机器人发送三条消息。

## 安装

```powershell
cd C:\Users\ROG\hostex-housekeeping-bot-asagaya
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## 配置

复制 `.env.example` 为 `.env`，然后填写 Hostex 账号、密码、企业微信机器人 webhook。

```powershell
copy .env.example .env
notepad .env
```

核心配置：

```env
HOSTEX_CLEANING_TARGET_ROOMS=阿佐谷A,阿佐谷B,阿佐谷C
HOSTEX_REMINDER_DAY_DIFFS=2,1,0
HOSTEX_CLEANING_TIMEZONE=Asia/Tokyo
```

`HOSTEX_REMINDER_DAY_DIFFS=2,1,0` 表示：退房清扫日前 2 天、前 1 天、当天都会发送。

如果只想退房当天发送：

```env
HOSTEX_REMINDER_DAY_DIFFS=0
```

## 运行

```powershell
python hostex_cleaning_reminder.py
```

## 发送效果

例如阿佐谷A和阿佐谷B都符合提醒条件，企业微信群里会收到两条独立消息：

```text
各位打扰了，阿佐谷A将在5月3号退房，麻烦各位安排一下打扫，多谢。
```

```text
各位打扰了，阿佐谷B将在5月3号退房，麻烦各位安排一下打扫，多谢。
```

如果识别到背靠背入住，会自动追加：

```text
注意这次客人背靠背，退房时间11AM，下一波客人入住时间16PM。
```
