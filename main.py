#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
sustech_grabber.py - 南科大TIS喵课助手 (最终决战版)

集成了安全稳定模式与性能至上模式，请按需选择。
祝你好运！

@CreateDate 2021-1-9
@UpdateDate 2025-08-13
"""

import sys
import os
import json
import re
import random
import math
from datetime import datetime, timedelta
import time
import asyncio
import threading
from getpass import getpass
import schedule

import requests
import aiohttp
from colorama import init, Fore, Style

# --- 全局配置 ---
# 模式2 (激进模式) 的核心性能参数
AGGRESSIVE_CONCURRENCY = 50  # 并发请求数 (建议 30-100)
AGGRESSIVE_TIMEOUT = 1.5      # 请求超时 (秒)
AGGRESSIVE_MAX_RETRIES = 3    # 最大重试次数
AGGRESSIVE_BACKOFF = 0.1      # 退避时间

# 模式1 (安全模式) 的核心性能参数
SAFE_INTERVAL = 1.0         # 轮询间隔 (秒) - 从2秒改为1秒
SAFE_MAX_BURST = 3          # 安全模式的突发请求数
SAFE_BURST_INTERVAL = 0.3   # 突发请求间隔 (秒)

# --- 通用常量 ---
CLASS_CACHE_PATH = "class.txt"
COURSE_INFO_PATH = "course.txt"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
CAS_LOGIN_URL = "https://cas.sustech.edu.cn/cas/login?service=https%3A%2F%2Ftis.sustech.edu.cn%2Fcas"
TIS_BASE_URL = "https://tis.sustech.edu.cn"
COURSE_TYPE_MAP = {'bxxk': "通识必修选课", 'xxxk': "通识选修选课", "kzyxk": '培养方案内课程',
                   "zynknjxk": '非培养方案内课程', "cxxk": '重修选课', "jhnxk": '计划内选课新生'}

# --- 初始化与全局变量 ---
init(autoreset=True)
SUCCESS = f"[{Fore.GREEN}+{Style.RESET_ALL}] "
STAR = f"[{Fore.GREEN}*{Style.RESET_ALL}] "
ERROR = f"[{Fore.RED}x{Style.RESET_ALL}] "
INFO = f"[{Fore.CYAN}!{Style.RESET_ALL}] "
FAIL = f"[{Fore.YELLOW}-{Style.RESET_ALL}] "

course_list_lock = threading.Lock()
stop_event = threading.Event()

# ==============================================================================
#  sección 1: 同步函数 (用于登录、信息获取和安全模式)
# ==============================================================================

def cas_login_sync(session, sid, pwd):
    """使用 requests (同步) 进行CAS登录。"""
    print(INFO + "正在连接CAS统一认证服务...")
    try:
        response = session.get(CAS_LOGIN_URL, timeout=10)
        response.raise_for_status()
        execution_match = re.search(r'name="execution" value="([^"]+)"', response.text)
        if not execution_match: return False

        payload = {'username': sid, 'password': pwd, 'execution': execution_match.group(1),
                   '_eventId': 'submit', 'geolocation': ''}
        
        print(INFO + "正在提交登录信息...")
        response = session.post(CAS_LOGIN_URL, data=payload, allow_redirects=False, timeout=10)
        
        if response.status_code == 302 and 'Location' in response.headers:
            print(SUCCESS + "CAS登录成功！正在跳转至TIS...")
            session.get(response.headers['Location'], timeout=10)
            return True
        return False
    except requests.exceptions.RequestException as e:
        print(ERROR + f"网络请求失败: {e}")
        return False

def fetch_all_courses_sync(session, semester_data):
    """使用 requests (同步) 获取并缓存所有课程信息。"""
    p_xnxq = semester_data['p_xnxq']
    if os.path.exists(COURSE_INFO_PATH):
        try:
            with open(COURSE_INFO_PATH, "r", encoding="utf8") as f:
                cache_content = json.load(f)
            if cache_content.get("semester") == p_xnxq:
                print(INFO + f"从缓存 '{COURSE_INFO_PATH}' 加载课程信息。")
                return cache_content['courses']
        except (json.JSONDecodeError, KeyError):
            print(FAIL + "缓存文件损坏，将从服务器重新获取。")

    print(INFO + "正在从服务器下载所有课程信息 (此过程可能需要一点时间)...")
    all_courses_info = {}
    for c_type, c_name in COURSE_TYPE_MAP.items():
        print(STAR + f"获取 {c_name} 列表...")
        # --- 补全所有必需的参数 ---
        payload = {
            "p_xn": semester_data['p_xn'],
            "p_xq": semester_data['p_xq'],
            "p_xnxq": p_xnxq,
            "p_pylx": 1,
            "mxpylx": 1,
            "p_xkfsdm": c_type,
            "pageNum": 1,
            "pageSize": 2000 
        }
        try:
            response = session.post(f"{TIS_BASE_URL}/Xsxk/queryKxrw", data=payload, timeout=20)
            data = response.json()
            if data.get('kxrwList') and data['kxrwList'].get('list'):
                for course in data['kxrwList']['list']:
                    all_courses_info[course['rwmc']] = {'id': course['id'], 'type': c_type}
        except Exception as e:
            print(ERROR + f"获取 {c_name} 列表失败: {e}")

    if not all_courses_info:
        # 添加一个检查，如果所有类别都获取失败，给出更明确的提示
        print(ERROR + "未能从任何课程类别中获取到数据，请检查TIS系统是否正在维护，或当前是否为非选课时段。")
    else:
        print(SUCCESS + f"所有课程信息获取完毕，共 {len(all_courses_info)} 门课程。")
        with open(COURSE_INFO_PATH, "w", encoding="utf8") as f:
            cache_data = {"semester": p_xnxq, "courses": all_courses_info}
            json.dump(cache_data, f, ensure_ascii=False, indent=4)
        print(SUCCESS + f"课程信息已缓存至 '{COURSE_INFO_PATH}'。")
        
    return all_courses_info
def attempt_register_sync(session, course_data, semester_data, retry_count=0):
    """同步的选课尝试函数 - 优化版。"""
    c_id, c_type, c_name = course_data
    payload = {"p_xktjz": "rwtjzyx", "p_xnxq": semester_data['p_xnxq'], "p_xkfsdm": c_type, "p_id": c_id}
    
    # 添加随机延迟防止检测
    if retry_count > 0:
        delay = random.uniform(0.1, 0.5) * retry_count
        time.sleep(delay)
    
    try:
        # 降低超时时间提高响应速度
        response = session.post(f"{TIS_BASE_URL}/Xsxk/addGouwuche", data=payload, timeout=3)
        result = response.json()
        message = result.get('message', '无返回信息')
        timestamp = time.strftime('%H:%M:%S')

        if "成功" in message:
            print(f"{Fore.GREEN}{Style.BRIGHT}[{timestamp}] {c_name} - 选课成功！{Style.RESET_ALL}")
            return True
        elif any(keyword in message for keyword in ["冲突", "已选", "已满", "不满足", "超学分"]):
            print(f"{Fore.YELLOW}[{timestamp}] {c_name} - {message} (永久性失败){Style.RESET_ALL}")
            return True
        else:
            print(f"{Fore.CYAN}[{timestamp}] {c_name} - {message} (继续尝试){Style.RESET_ALL}")
            return False
    except Exception as e:
        print(f"{Fore.RED}[{time.strftime('%H:%M:%S')}] {c_name} - 请求异常: {e}{Style.RESET_ALL}")
        return False

def run_mode_safe(session, course_list, semester_data):
    """模式1：安全稳定模式的运行逻辑 - 优化版。"""
    print("\n" + "="*20 + " 模式1：安全稳定模式 " + "="*20)
    print(INFO + f"将以 {SAFE_INTERVAL} 秒的间隔轮询所有课程，突发模式可发送 {SAFE_MAX_BURST} 个连续请求。按 Ctrl+C 退出。")
    
    while not stop_event.is_set():
        with course_list_lock:
            if not course_list:
                print(SUCCESS + "所有课程已处理完毕！")
                break
            targets = course_list[:]
        
        courses_to_remove = []
        for course in targets:
            if stop_event.is_set(): 
                break
                
            # 突发模式 - 对每个课程连续尝试多次
            success = False
            for burst_attempt in range(SAFE_MAX_BURST):
                if attempt_register_sync(session, course, semester_data, burst_attempt):
                    success = True
                    break
                if burst_attempt < SAFE_MAX_BURST - 1:  # 不是最后一次尝试
                    time.sleep(SAFE_BURST_INTERVAL)
            
            if success:
                courses_to_remove.append(course)
            
            # 主要间隔时间
            if not stop_event.is_set():
                time.sleep(SAFE_INTERVAL)
        
        if courses_to_remove:
            with course_list_lock:
                for course in courses_to_remove:
                    if course in course_list:
                        course_list.remove(course)

# ==============================================================================
# sección 2: 异步函数 (用于性能至上模式)
# ==============================================================================

async def cas_login_async(sid, pwd):
    """使用 aiohttp (异步) 进行登录并返回一个已认证的 session - 优化版。"""
    print(INFO + "正在为激进模式初始化异步会话...")
    timeout = aiohttp.ClientTimeout(total=AGGRESSIVE_TIMEOUT, connect=1.0)
    connector = aiohttp.TCPConnector(
        limit=100,  # 提高连接池大小
        limit_per_host=50,
        keepalive_timeout=30,
        enable_cleanup_closed=True
    )
    session = aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT}, 
        timeout=timeout,
        connector=connector
    )
    
    try:
        async with session.get(CAS_LOGIN_URL, ssl=False) as response:
            text = await response.text()
            execution_match = re.search(r'name="execution" value="([^"]+)"', text)
            if not execution_match: 
                return None
            
            payload = {'username': sid, 'password': pwd, 'execution': execution_match.group(1),
                       '_eventId': 'submit', 'geolocation': ''}

            async with session.post(CAS_LOGIN_URL, data=payload, allow_redirects=False, ssl=False) as post_response:
                if post_response.status == 302:
                    await session.get(post_response.headers['Location'], ssl=False)
                    print(SUCCESS + "异步会话认证成功！")
                    return session
                return None
    except Exception as e:
        print(ERROR + f"异步登录失败: {e}")
        await session.close()
        return None

async def attempt_register_async(session, course_data, semester_data, worker_id, semaphore):
    """异步的选课尝试函数 - 优化版。"""
    c_id, c_type, c_name = course_data
    payload = {"p_xktjz": "rwtjzyx", "p_xnxq": semester_data['p_xnxq'], "p_xkfsdm": c_type, "p_id": c_id}
    
    async with semaphore:  # 限制并发数
        retry_count = 0
        while retry_count < AGGRESSIVE_MAX_RETRIES:
            try:
                # 添加微小的随机延迟以避免同时请求
                if retry_count > 0:
                    await asyncio.sleep(random.uniform(0.05, 0.2))
                
                async with session.post(f"{TIS_BASE_URL}/Xsxk/addGouwuche", data=payload, ssl=False) as response:
                    result = await response.json(content_type=None)
                    message = result.get('message', '')
                    ts = time.strftime('%H:%M:%S')

                    if "成功" in message:
                        print(f"{Fore.GREEN}{Style.BRIGHT}[{ts}][W-{worker_id:02d}] {c_name} - 选课成功！{Style.RESET_ALL}")
                        return 'SUCCESS'
                    elif any(k in message for k in ["冲突", "已选", "已满", "不满足", "超学分"]):
                        print(f"{Fore.YELLOW}[{ts}][W-{worker_id:02d}] {c_name} - {message} (永久失败){Style.RESET_ALL}")
                        return 'PERM_FAIL'
                    else:
                        print(f"{Fore.CYAN}[{ts}][W-{worker_id:02d}] {c_name} - {message} (继续){Style.RESET_ALL}")
                        retry_count += 1
                        if retry_count < AGGRESSIVE_MAX_RETRIES:
                            await asyncio.sleep(AGGRESSIVE_BACKOFF * retry_count)
                        continue
                        
            except asyncio.TimeoutError:
                retry_count += 1
                print(f"{Fore.RED}[{time.strftime('%H:%M:%S')}][W-{worker_id:02d}] {c_name} - 超时，重试 {retry_count}/{AGGRESSIVE_MAX_RETRIES}{Style.RESET_ALL}")
                if retry_count < AGGRESSIVE_MAX_RETRIES:
                    await asyncio.sleep(AGGRESSIVE_BACKOFF * retry_count)
            except Exception as e:
                retry_count += 1
                print(f"{Fore.RED}[{time.strftime('%H:%M:%S')}][W-{worker_id:02d}] {c_name} - 异常: {e}, 重试 {retry_count}/{AGGRESSIVE_MAX_RETRIES}{Style.RESET_ALL}")
                if retry_count < AGGRESSIVE_MAX_RETRIES:
                    await asyncio.sleep(AGGRESSIVE_BACKOFF * retry_count)
        
        return 'TEMP_FAIL'

async def worker_async(worker_id, session, semester_data, queue, semaphore, stats):
    """异步模式的工人 - 优化版。"""
    while True:
        try:
            course_data = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            # 没有更多任务，继续等待
            continue
        
        status = await attempt_register_async(session, course_data, semester_data, worker_id, semaphore)
        
        # 统计信息
        stats['total_attempts'] += 1
        
        if status == 'SUCCESS':
            stats['success_count'] += 1
            # 成功后不再放回队列
        elif status == 'PERM_FAIL':
            stats['perm_fail_count'] += 1
            # 永久失败也不再放回队列
        else:  # TEMP_FAIL
            stats['temp_fail_count'] += 1
            await queue.put(course_data)  # 放回队列继续尝试
            # 添加微小延迟避免过于频繁的重试
            await asyncio.sleep(random.uniform(0.1, 0.3))
        
        queue.task_done()

async def run_mode_aggressive(sid, pwd, course_list, semester_data):
    """模式2：性能至上模式的运行逻辑 - 优化版。"""
    print("\n" + "="*20 + " 模式2：性能至上模式 " + "="*20)
    print(f"{FAIL}警告: 您正在使用高风险模式。并发数: {AGGRESSIVE_CONCURRENCY}。")
    print(f"{FAIL}此模式会忽略SSL证书验证，请确保网络环境安全！")
    
    session = await cas_login_async(sid, pwd)
    if not session:
        print(ERROR + "为激进模式创建异步会话失败，请检查网络或CAS状态。")
        return
        
    queue = asyncio.Queue(maxsize=1000)  # 限制队列大小
    for course in course_list:
        await queue.put(course)

    # 使用信号量控制并发数
    semaphore = asyncio.Semaphore(AGGRESSIVE_CONCURRENCY)
    
    # 统计信息
    stats = {
        'total_attempts': 0,
        'success_count': 0,
        'perm_fail_count': 0,
        'temp_fail_count': 0
    }

    print(f"{INFO}启动 {AGGRESSIVE_CONCURRENCY} 个并发任务... 按 Ctrl+C 停止。")
    
    tasks = []
    for i in range(AGGRESSIVE_CONCURRENCY):
        task = asyncio.create_task(worker_async(i + 1, session, semester_data, queue, semaphore, stats))
        tasks.append(task)
    
    # 创建一个统计任务
    async def print_stats():
        while True:
            await asyncio.sleep(10)  # 每10秒打印一次统计
            print(f"{INFO}统计: 总请求 {stats['total_attempts']}, 成功 {stats['success_count']}, 永久失败 {stats['perm_fail_count']}, 临时失败 {stats['temp_fail_count']}")
    
    stats_task = asyncio.create_task(print_stats())
    
    try:
        await queue.join()  # 等待所有课程被处理
    except KeyboardInterrupt:
        print(f"\n{INFO}收到用户中断信号。")
    finally:
        # 取消所有任务
        stats_task.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, stats_task, return_exceptions=True)
        
        await session.close()
        print(f"\n{SUCCESS}所有课程均已处理（成功或永久失败），任务结束。")
        print(f"{INFO}最终统计: 总请求 {stats['total_attempts']}, 成功 {stats['success_count']}, 永久失败 {stats['perm_fail_count']}, 临时失败 {stats['temp_fail_count']}")

def get_user_config():
    """获取用户自定义配置。"""
    global AGGRESSIVE_CONCURRENCY, SAFE_INTERVAL, SAFE_MAX_BURST
    
    print(f"\n{INFO}当前配置:")
    print(f"  激进模式并发数: {AGGRESSIVE_CONCURRENCY}")
    print(f"  安全模式间隔: {SAFE_INTERVAL}秒")
    print(f"  安全模式突发数: {SAFE_MAX_BURST}")
    
    if input(f"\n是否要修改配置? (y/N): ").lower() == 'y':
        try:
            new_concurrency = input(f"输入激进模式并发数 (当前: {AGGRESSIVE_CONCURRENCY}, 建议 30-100): ")
            if new_concurrency.strip():
                AGGRESSIVE_CONCURRENCY = max(1, min(200, int(new_concurrency)))
                
            new_interval = input(f"输入安全模式间隔秒数 (当前: {SAFE_INTERVAL}, 建议 0.5-3): ")
            if new_interval.strip():
                SAFE_INTERVAL = max(0.1, min(10, float(new_interval)))
                
            new_burst = input(f"输入安全模式突发数 (当前: {SAFE_MAX_BURST}, 建议 1-5): ")
            if new_burst.strip():
                SAFE_MAX_BURST = max(1, min(10, int(new_burst)))
                
            print(f"{SUCCESS}配置已更新!")
        except ValueError:
            print(f"{ERROR}输入格式错误，使用默认配置。")

def wait_until_start_time(start_datetime):
    """等待到指定的开始时间。"""
    now = datetime.now()
    if now >= start_datetime:
        print(f"{INFO}已到达开始时间，立即开始抢课！")
        return
    
    wait_seconds = (start_datetime - now).total_seconds()
    print(f"{INFO}距离开始时间还有 {wait_seconds:.0f} 秒 ({start_datetime.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"{INFO}脚本将自动等待，请保持程序运行...")
    
    # 显示倒计时
    while True:
        now = datetime.now()
        if now >= start_datetime:
            print(f"\n{SUCCESS}时间到！开始抢课！")
            break
        
        remaining = (start_datetime - now).total_seconds()
        if remaining <= 0:
            break
            
        # 每秒更新一次倒计时显示
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        seconds = int(remaining % 60)
        print(f"\r{INFO}倒计时: {hours:02d}:{minutes:02d}:{seconds:02d}", end="", flush=True)
        time.sleep(1)
    
    print()  # 换行

def get_scheduled_start_time():
    """获取用户设置的定时开始时间。"""
    print(f"\n{INFO}定时抢课功能")
    print("请输入抢课开放时间，脚本将提前10分钟自动开始运行")
    
    while True:
        try:
            # 获取日期
            date_input = input("请输入日期 (格式: YYYY-MM-DD，如 2025-09-07): ").strip()
            if not date_input:
                return None
            
            # 获取时间
            time_input = input("请输入时间 (格式: HH:MM，如 13:00): ").strip()
            if not time_input:
                return None
            
            # 解析日期时间
            datetime_str = f"{date_input} {time_input}"
            target_datetime = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M")
            
            # 提前10分钟
            start_datetime = target_datetime - timedelta(minutes=10)
            
            print(f"{SUCCESS}设置成功！")
            print(f"  抢课开放时间: {target_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  脚本开始时间: {start_datetime.strftime('%Y-%m-%d %H:%M:%S')} (提前10分钟)")
            
            # 检查时间是否合理
            now = datetime.now()
            if start_datetime <= now:
                print(f"{FAIL}警告: 设置的时间已经过去或即将到达，脚本将立即开始运行")
            
            confirm = input(f"\n确认使用此时间设置吗? (y/N): ").strip().lower()
            if confirm == 'y':
                return start_datetime
            else:
                print(f"{INFO}重新设置时间...")
                
        except ValueError as e:
            print(f"{ERROR}时间格式错误，请重新输入。错误: {e}")
        except Exception as e:
            print(f"{ERROR}输入处理失败: {e}")

def main():
    """主程序"""
    print("="*20 + " 南科大TIS喵课助手 " + "="*20)
    print(f"{FAIL}本脚本仅供学习交流使用，滥用可能导致账号被封禁，后果自负。")

    if not os.path.exists(CLASS_CACHE_PATH):
        print(ERROR + f"未找到课程列表文件 '{CLASS_CACHE_PATH}'，请创建并填入课程全名。")
        return
    with open(CLASS_CACHE_PATH, "r", encoding="utf8") as f:
        target_course_names = [line.strip() for line in f if line.strip()]
    if not target_course_names:
        print(ERROR + f"'{CLASS_CACHE_PATH}' 为空，无目标课程。")
        return

    # 定时功能选择
    print(f"\n{INFO}是否要使用定时抢课功能？")
    use_schedule = input("输入 'y' 启用定时功能，直接回车立即开始: ").strip().lower()
    
    start_time = None
    if use_schedule == 'y':
        start_time = get_scheduled_start_time()
        if start_time is None:
            print(f"{INFO}取消定时设置，直接开始抢课")

    # 登录部分
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    
    sid = input("请输入您的学号: ")
    pwd = getpass("请输入CAS密码 (输入时不可见): ")
    
    # 如果设置了定时，先登录验证账户
    if start_time:
        print(f"\n{INFO}正在验证登录信息...")
        if not cas_login_sync(session, sid, pwd):
            print(ERROR + "登录失败，请检查账户密码后重试。")
            return
        print(f"{SUCCESS}账户验证成功！")
        
        # 等待到开始时间
        wait_until_start_time(start_time)
        
        # 重新登录（防止会话过期）
        print(f"{INFO}重新登录以确保会话有效...")
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        if not cas_login_sync(session, sid, pwd):
            print(ERROR + "重新登录失败，程序退出。")
            return
    else:
        # 直接登录
        if not cas_login_sync(session, sid, pwd):
            print(ERROR + "登录失败，程序退出。")
            return

    try:
        response = session.post(f"{TIS_BASE_URL}/Xsxk/queryXkdqXnxq", data={"mxpylx": 1}, timeout=10)
        semester_info = response.json()
        print(SUCCESS + f"当前学期: {semester_info['p_xn']}学年 第{semester_info['p_xq']}学期")
    except Exception as e:
        print(ERROR + f"获取学期信息失败: {e}")
        return

    all_courses = fetch_all_courses_sync(session, semester_info)
    if not all_courses:
        print(ERROR + "获取课程总表失败，程序退出。")
        return

    final_course_list = []
    for name in target_course_names:
        if name in all_courses:
            info = all_courses[name]
            final_course_list.append([info['id'], info['type'], name])
        else:
            print(FAIL + f"警告: 课程 '{name}' 在本学期所有课程列表中未找到。")
    
    if not final_course_list:
        print(ERROR + "所有目标课程均无效，程序退出。")
        return

    print("\n" + "="*20 + " 待抢课程列表确认 " + "="*20)
    for c_id, c_type, c_name in final_course_list:
        print(f"{SUCCESS}{c_name:<30} (ID: {c_id})")
    print("="*55)
    
    # 5. 模式选择
    while True:
        print("\n请选择运行模式:")
        print(f"  [{Fore.CYAN}1{Style.RESET_ALL}] {Fore.GREEN}安全稳定模式{Style.RESET_ALL} (适合捡漏，低风险，1秒间隔可突发{SAFE_MAX_BURST}次请求)")
        print(f"  [{Fore.CYAN}2{Style.RESET_ALL}] {Fore.RED}性能至上模式{Style.RESET_ALL} (适合开抢瞬间，高风险，{AGGRESSIVE_CONCURRENCY}个并发请求)")
        print(f"  [{Fore.CYAN}0{Style.RESET_ALL}] 退出")
        mode = input("请输入你的选择: ")
        
        if mode == '1':
            run_mode_safe(session, final_course_list, semester_info)
            break
        elif mode == '2':
            try:
                # 激进模式需要重新传入sid和pwd以创建独立的异步会话
                asyncio.run(run_mode_aggressive(sid, pwd, final_course_list, semester_info))
            except KeyboardInterrupt:
                print("\n" + INFO + "收到用户中断信号。")
            finally:
                break
        elif mode == '0':
            break
        else:
            print(ERROR + "输入无效，请重新输入。")

if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n" + INFO + "程序已退出。")
    finally:
        print(f"\n{STAR}祝你抢课成功，心想事成！")