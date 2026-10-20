"""PotatoCode 入口：装配日志出口、加载配置、启动 TUI。

banner 由 TUI 在 on_mount 时写入对话区（见 tui/app.py），此处不重复打印。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path

from . import __version__, config, hook, instructions, mcp, memory, permission, session
from .agent import new_runtime
from .command.skill_register import build_skill_catalog_text
from .compact import SessionContext, new_session_context, sessions_root
from .config import ConfigError
from .hook import Event as HookEvent
from .logging_setup import close_logging, setup_logging
from .memory import project_memory_dir, user_memory_dir
from .skills import SkillExecutor, SkillLoader
from .tool import new_default_registry
from .tool.install_skill import InstallSkillTool
from .tool.load_skill import LoadSkill
from .tui import PotatoCodeApp

logger = logging.getLogger(__name__)

# 运行期配置文件路径（含真实密钥，已被 .gitignore 忽略）
CONFIG_PATH = ".potatocode/config.yaml"

# 会话保留期：超过即由启动时的后台清理任务删除
SESSION_MAX_AGE = timedelta(days=30)


async def _amain() -> int:
    """异步入口：先建会话与日志出口，再走主体流程，最后收尾出口。

    会话标识必须早于配置读取诞生——配置错误、MCP 连接失败等启动早期事件也要能
    落进该会话的日志（docs/ch10/spec.md F2）。日志出口装配失败时降级为无日志运行，
    不阻断启动（N4）。
    """
    root = str(Path.cwd().resolve())
    ses_ctx = new_session_context(root)
    handler = setup_logging(ses_ctx.log_path)
    logger.info("启动：session=%s workspace=%s", ses_ctx.session_id, root)
    try:
        return await _run(root, ses_ctx)
    finally:
        close_logging(handler)  # 刷新并关闭出口，保证日志尾部不残缺（F9）


async def _run(root: str, ses_ctx: SessionContext) -> int:
    """主体流程：配置 → 指令/记忆 → MCP → 权限引擎 → 运行时 → TUI。

    会话沿用启动时建好的那一份，使日志文件、JSONL 存档、工具结果落盘同处一个会话目录。
    """
    try:
        cfg = config.load(CONFIG_PATH)
    except ConfigError as e:
        # N4：可读错误提示，而非崩溃堆栈；同时留一条日志便于事后追溯（F2）
        logger.error("配置错误: %s", e)
        print(f"配置错误: {e}", file=sys.stderr)
        return 1

    # 项目指令与长期记忆：进程启动时各加载一次，之后按需注入系统提示（ch11 F8/F32）
    instruction_text = instructions.Loader(root).load()
    mem_mgr = memory.Manager(project_memory_dir(root), user_memory_dir())
    memory_text = mem_mgr.load_index()
    logger.info("指令 %d 字符，记忆索引 %d 字符", len(instruction_text), len(memory_text))

    # 会话存档写入器：每条消息实时追加到 conversation.jsonl（ch11 F13/F15）
    writer = session.Writer(ses_ctx.session_dir)
    # 过期会话清理放后台任务，不阻塞启动（ch11 F26）
    cleanup = asyncio.create_task(
        asyncio.to_thread(session.clean_expired, sessions_root(root), SESSION_MAX_AGE)
    )
    cleanup.add_done_callback(_log_cleanup)

    registry = new_default_registry()
    # ch13 Skill 系统：先扫描两级 skill 目录，再把 LoadSkill / InstallSkill 注册为
    # 内置工具（在 Agent 构造前进注册中心，保证首轮请求就能看到 LoadSkill 定义）
    skill_loader = SkillLoader(root)
    skill_loader.load_all()
    skill_executor = SkillExecutor(skill_loader)
    load_skill_tool = LoadSkill()
    load_skill_tool.set_loader(skill_loader)
    load_skill_tool.set_executor(skill_executor)  # fork 分支后台启动子 Agent 用
    registry.register(load_skill_tool)
    install_skill_tool = InstallSkillTool(skill_loader)
    registry.register(install_skill_tool)
    loaded_skill_names = ", ".join(skill_loader.names())
    logger.info("已加载 %d 个 skill: %s", len(skill_loader.names()), loaded_skill_names)
    manager = await mcp.new_manager(mcp.load_config(root), version=__version__)
    try:
        for remote_tool in manager.tools():
            registry.register(remote_tool)
        # 权限引擎（五层防御的前四层）：根于项目目录；解析失败仅降级提示、继续启动
        engine, err = permission.new_engine(root)
        if err is not None:
            logger.warning("权限引擎降级: %s", err)
            print(f"权限引擎降级: {err}", file=sys.stderr)
        # ch14 Hook 引擎：扫描两层 hooks.yaml；加载错误一律 stderr、不阻断启动（N1）
        hook_engine = hook.load(root)
        logger.info("已加载 %d 条 hook（来源：%s）", len(hook_engine.rules), hook_engine.sources)
        # 会话级运行时：复用启动时建好的会话，使日志文件与运行时指向同一会话（F1/F2）
        runtime = new_runtime(workspace=root, session=ses_ctx)
        app = PotatoCodeApp(
            cfg.providers,
            __version__,
            registry,
            engine,
            manager,
            runtime=runtime,
            writer=writer,
            mem_mgr=mem_mgr,
            instruction_text=instruction_text,
            memory_text=memory_text,
            sessions_dir=sessions_root(root),
            workspace=root,
            skill_loader=skill_loader,
            skill_executor=skill_executor,
            load_skill_tool=load_skill_tool,
            skill_catalog_text=build_skill_catalog_text(skill_loader),
            hook_engine=hook_engine,
        )
        # skill 安装完成后重建斜杠命令 + 刷新 Agent 目录（无需重启即可使用，F19）
        install_skill_tool.set_on_installed(app.rebind_skill_commands)
        await app.run_async()
        # ch14：App 退出后的 SessionEnd 兜底（Ctrl+C 等退出路径也 emit，F9/T20）
        await hook_engine.dispatch(
            HookEvent.SESSION_END,
            {
                "event": HookEvent.SESSION_END.value,
                "session_id": runtime.session.session_id,
                "cwd": root,
                "mode": app.mode.name.lower(),
            },
        )
    except KeyboardInterrupt:
        pass  # 终端层的 Ctrl+C，安静退出
    finally:
        writer.close()  # TUI 已退出，存档句柄在此兜底关闭
        await manager.close()
    return 0


def _log_cleanup(task: asyncio.Task) -> None:
    """记录后台清理结果（被取消或失败都不影响主流程）。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("过期会话清理失败: %s", exc)


def main() -> None:
    """CLI 同步入口。"""
    raise SystemExit(asyncio.run(_amain()))
