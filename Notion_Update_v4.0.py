# coding=utf-8

import os
import time # 引入 time 模块用于防封号等待
import google.generativeai as genai # 引入 AI 模块
from Util.FeedTool import NotionAPI, parse_rss_entries
import requests

# 从环境变量中获取配置
NOTION_API_KEY = os.getenv('NOTION_API_KEY')
NOTION_READING_DATABASE_ID = os.getenv('NOTION_READING_DATABASE_ID')
NOTION_URL_DATABASE_ID = os.getenv('NOTION_URL_DATABASE_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY') # 获取 Gemini Key

# --- 请复制以下代码，替换原文件中配置 Gemini 的那一段 ---
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        
        # --- 调试代码：查看所有可用模型 ---
        print("====== 正在查询 Google AI 可用模型 ======")
        valid_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                print(f"发现模型: {m.name}")
                valid_models.append(m.name)
        print("=========================================")
        
        # 优先使用 flash，如果列表中没有，尝试使用 pro
        target_model = 'gemini-1.5-flash'
        
        # 简单的自动降级逻辑
        # 注意：API有时候返回 'models/gemini-1.5-flash'，有时候是 'gemini-1.5-flash'
        # 所以我们需要模糊匹配一下
        is_flash_available = any('gemini-1.5-flash' in m for m in valid_models)
        
        if not is_flash_available:
            print(f"警告: 列表中未找到 {target_model}，自动切换为 gemini-pro")
            target_model = 'gemini-pro'
            
        model = genai.GenerativeModel(target_model)
        print(f"Gemini 配置成功，当前使用模型: {target_model}")

    except Exception as e:
        print(f"Gemini 配置发生错误: {e}")
        model = None
else:
    model = None
    print("Warning: 未检测到 GEMINI_API_KEY，AI 总结功能将不启用。")
# ----------------------------------------------------

def update():

    if NOTION_API_KEY is None:
        print("NOTION_SEC secrets is not set!")
        return

    api = NotionAPI(NOTION_API_KEY, NOTION_READING_DATABASE_ID, NOTION_URL_DATABASE_ID)

    rss_feed_list = api.queryFeed_from_notion()

    for rss_feed in rss_feed_list:
        # --- 修复报错：检查 URL 是否为空 ---
        url = rss_feed.get("url")
        if not url:
            print(f"跳过无效的 RSS 源 (URL为空): {rss_feed.get('title', 'Unknown')}")
            continue
        
        try:
            feeds, entries = parse_rss_entries(url)
        except Exception as e:
            print(f"解析 RSS 失败 ({url}): {e}")
            continue
        # --------------------------------

        rss_page_id = rss_feed.get("page_id")
        
        # 如果没有新文章，更新一下 Feed 信息就跳过
        if len(entries) == 0:
            api.saveFeed_to_notion(feeds, page_id=rss_page_id)
            continue
        
        # Check for Repeat Entries (去重逻辑)
        url_query = f"{api.NOTION_API_database}/{api.reader_id}/query"
        payload = {
            "filter": {
                "property": "Source",
                "relation": {"contains": rss_page_id},
            },
        }
        
        try:
            response = requests.post(url=url_query, headers=api.headers, json=payload)
            response.raise_for_status() # 检查请求是否成功
            
            # 获取当前数据库里已有的 URL 列表
            current_urls = []
            results = response.json().get("results", [])
            for x in results:
                # 防御性编程：防止 Notion 里某些行 URL 字段缺失导致报错
                props = x.get("properties", {})
                url_prop = props.get("URL", {})
                if url_prop and url_prop.get("url"):
                    current_urls.append(url_prop.get("url"))
        except Exception as e:
            print(f"查询 Notion 已有文章失败: {e}")
            current_urls = []
        
        repeat_flag = 0
        rss_tags = rss_feed.get("tags")
        
        # 更新 Feed 的最新时间等状态
        api.saveFeed_to_notion(feeds, page_id=rss_page_id)
        
        # 开始处理每一篇文章
        for entry in entries:
            # 只有当文章是新的（不在 current_urls 里）才处理
            if entry.get("link") not in current_urls:
                
                # === AI 分析模块开始 ===
                if model:
                    try:
                        # 提取文章内容用于分析 (优先取摘要，如果没有摘要取前500字)
                        content_to_analyze = entry.get("summary", "")
                        if not content_to_analyze:
                            content_to_analyze = entry.get("title", "")
                            
                        # 截断过长内容防止报错
                        content_to_analyze = content_to_analyze[:2000] 
                        
                        prompt = f"""
                        任务：分析这条新闻。
                        1. 给出重要性评分（1-10分）。
                        2. 用中文一句话总结核心内容。
                        3. 格式必须严格如下：【AI评分:8/10】这里是总结内容...
                        
                        新闻标题：{entry.get("title")}
                        新闻内容：
                        {content_to_analyze}
                        """
                        
                        print(f"正在 AI 分析: {entry.get('title')[:15]}...")
                        ai_response = model.generate_content(prompt)
                        ai_text = ai_response.text.strip()
                        
                        # 【关键策略】
                        # 为了不需要修改 Util/FeedTool.py 也能看到结果
                        # 我们把 AI 结果“拼”到原有的摘要前面
                        original_summary = entry.get("summary", "")
                        entry["summary"] = f"{ai_text}\n\n{original_summary}"
                        
                        # 强制休息 4 秒，防止 Google 429 报错 (免费版限制每分钟15次请求)
                        time.sleep(4)
                        
                    except Exception as e:
                        print(f"AI 生成失败 (跳过AI部分): {e}")
                        # 失败了不影响保存，只是没 AI 摘要而已
                        time.sleep(1) # 发生错误也稍微停一下
                # === AI 分析模块结束 ===

                # 保存到 Notion
                api.saveEntry_to_notion(entry, rss_page_id, rss_tags)
                
                # 把新存的 URL 加入列表，防止同一次运行中重复处理
                current_urls.append(entry.get("link"))
            else:
                repeat_flag += 1

        print(f"[{rss_feed.get('title')}] 读取 {len(entries)} 篇，重复 {repeat_flag} 篇。")

if __name__ == "__main__":
    update()
