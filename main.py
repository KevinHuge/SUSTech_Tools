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
import time
import asyncio
import threading
from getpass import getpass

import requests
import aiohttp
from colorama import init, Fore, Style

# --- 全局配置 ---
# 模式2 (激进模式) 的核心性能参数
AGGRESSIVE_CONCURRENCY = 25  # 并发请求数 (建议 15-50)
AGGRESSIVE_TIMEOUT = 2.0      # 请求超时 (秒)

# 模式1 (安全模式) 的核心性能参数
SAFE_INTERVAL = 2           # 轮询间隔 (秒)

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
def attempt_register_sync(session, course_data, semester_data):
    """同步的选课尝试函数。"""
    c_id, c_type, c_name = course_data
    payload = {"p_xktjz": "rwtjzyx", "p_xnxq": semester_data['p_xnxq'], "p_xkfsdm": c_type, "p_id": c_id}
    try:
        response = session.post(f"{TIS_BASE_URL}/Xsxk/addGouwuche", data=payload, timeout=5)
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
    """模式1：安全稳定模式的运行逻辑。"""
    print("\n" + "="*20 + " 模式1：安全稳定模式 " + "="*20)
    print(INFO + f"将以 {SAFE_INTERVAL} 秒的间隔轮询所有课程。按 Ctrl+C 退出。")
    
    while not stop_event.is_set():
        with course_list_lock:
            if not course_list:
                print(SUCCESS + "所有课程已处理完毕！")
                break
            targets = course_list[:]
        
        courses_to_remove = []
        for course in targets:
            if stop_event.is_set(): break
            if attempt_register_sync(session, course, semester_data):
                courses_to_remove.append(course)
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
    """使用 aiohttp (异步) 进行登录并返回一个已认证的 session。"""
    print(INFO + "正在为激进模式初始化异步会话...")
    timeout = aiohttp.ClientTimeout(total=AGGRESSIVE_TIMEOUT)
    session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=timeout)
    
    # 此处省略了详细的输出，因为用户已在同步模式下登录过一次
    try:
        async with session.get(CAS_LOGIN_URL, ssl=False) as response:
            text = await response.text()
            execution_match = re.search(r'name="execution" value="([^"]+)"', text)
            if not execution_match: return None
            
            payload = {'username': sid, 'password': pwd, 'execution': execution_match.group(1),
                       '_eventId': 'submit', 'geolocation': ''}

            async with session.post(CAS_LOGIN_URL, data=payload, allow_redirects=False, ssl=False) as post_response:
                if post_response.status == 302:
                    await session.get(post_response.headers['Location'], ssl=False)
                    print(SUCCESS + "异步会话认证成功！")
                    return session
                return None
    except Exception:
        return None

async def attempt_register_async(session, course_data, semester_data, worker_id):
    """异步的选课尝试函数。"""
    c_id, c_type, c_name = course_data
    payload = {"p_xktjz": "rwtjzyx", "p_xnxq": semester_data['p_xnxq'], "p_xkfsdm": c_type, "p_id": c_id}
    try:
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
                return 'TEMP_FAIL'
    except Exception:
        return 'TEMP_FAIL'

async def worker_async(worker_id, session, semester_data, queue):
    """异步模式的工人。"""
    while True:
        course_data = await queue.get()
        status = await attempt_register_async(session, course_data, semester_data, worker_id)
        if status == 'TEMP_FAIL':
            await queue.put(course_data) # 放回队列继续尝试
        queue.task_done()

async def run_mode_aggressive(sid, pwd, course_list, semester_data):
    """模式2：性能至上模式的运行逻辑。"""
    print("\n" + "="*20 + " 模式2：性能至上模式 " + "="*20)
    print(f"{FAIL}警告: 您正在使用高风险模式。并发数: {AGGRESSIVE_CONCURRENCY}。")
    print(f"{FAIL}此模式会忽略SSL证书验证，请确保网络环境安全！")
    
    session = await cas_login_async(sid, pwd)
    if not session:
        print(ERROR + "为激进模式创建异步会话失败，请检查网络或CAS状态。")
        return
        
    queue = asyncio.Queue()
    for course in course_list:
        await queue.put(course)

    print(f"{INFO}启动 {AGGRESSIVE_CONCURRENCY} 个并发任务... 按 Ctrl+C 停止。")
    
    tasks = []
    for i in range(AGGRESSIVE_CONCURRENCY):
        task = asyncio.create_task(worker_async(i + 1, session, semester_data, queue))
        tasks.append(task)
        
    await queue.join() # 等待所有课程被处理
    
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    
    await session.close()
    print(f"\n{SUCCESS}所有课程均已处理（成功或永久失败），任务结束。")

# ==============================================================================
# sección 3: 主程序入口
# ==============================================================================

def main():
    """主程序"""
    print("="*20 + " 南科大TIS喵课助手 " + "="*20)
    print(f"{FAIL}本脚本仅供学习交流使用，滥用可能导致账号被封禁，后果自负。")

    # 1. 加载目标课程
    if not os.path.exists(CLASS_CACHE_PATH):
        print(ERROR + f"未找到课程列表文件 '{CLASS_CACHE_PATH}'，请创建并填入课程全名。")
        return
    with open(CLASS_CACHE_PATH, "r", encoding="utf8") as f:
        target_course_names = [line.strip() for line in f if line.strip()]
    if not target_course_names:
        print(ERROR + f"'{CLASS_CACHE_PATH}' 为空，无目标课程。")
        return

    # 2. 同步登录
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    
    sid = input("请输入您的学号: ")
    pwd = getpass("请输入CAS密码 (输入时不可见): ")
    if not cas_login_sync(session, sid, pwd):
        print(ERROR + "登录失败，程序退出。")
        return

    # 3. 获取学期和课程信息
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

    # 4. 准备最终待抢列表
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
        print(f"  [{Fore.CYAN}1{Style.RESET_ALL}] {Fore.GREEN}安全稳定模式{Style.RESET_ALL} (适合捡漏，低风险)")
        print(f"  [{Fore.CYAN}2{Style.RESET_ALL}] {Fore.RED}性能至上模式{Style.RESET_ALL} (适合开抢瞬间，高风险)")
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