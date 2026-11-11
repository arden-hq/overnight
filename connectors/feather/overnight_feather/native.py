import os
import re
from contextlib import contextmanager
from pathlib import Path

from feather_harness import FileTools, ProcessTools, TodoList
from feather_harness.local import process as feather_process
from feather_harness.local.filesystem import LocalFileRuntime
from feather_harness.local.process import LocalShellRuntime
from feather_harness.production import (
    build_production_instructions,
    production_tools,
)
from feather_harness.prompt import build_instructions
from feather_harness.scripts.install_skills import (
    install_production_skills,
    install_skills,
    install_task_skills,
)
from feather_harness.tools.web import web_fetch_tool, web_search_tool
from feather_harness.tools.web.exa import ExaWebProvider

from overnight_feather.options import NativeOptions

SECRET_ENVIRONMENT = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)|^OVERNIGHT_", re.I)


def _scrub_environment(environment: dict[str, str]) -> dict[str, str]:
    return {
        name: value for name, value in environment.items() if not SECRET_ENVIRONMENT.search(name)
    }


class ScrubbedShellRuntime(LocalShellRuntime):
    async def __aenter__(self):
        original = feather_process._task_environment
        if not getattr(original, "overnight_scrubbed", False):

            @contextmanager
            def scrubbed(task_python):
                with original(task_python) as environment:
                    yield _scrub_environment(environment)

            scrubbed.overnight_scrubbed = True
            feather_process._task_environment = scrubbed
        return await super().__aenter__()


async def configure(
    options: NativeOptions,
    directory: Path,
    stack,
    instructions: str,
    *,
    workspace: Path | None = None,
    install: bool = True,
):
    compaction = options.effective_compaction
    if options.profile == "world":
        return (
            (),
            instructions + "\n\nNo native file/process/web tools are configured.",
            compaction,
        )
    workspace = workspace or directory / "workspace"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    if install:
        install_skills(workspace, overwrite=True)
        if options.profile == "prod":
            install_production_skills(workspace)
        if options.skills is not None:
            install_task_skills(workspace, options.skills)
    instructions += (
        "\n\nUse native file/process tools in the configured workspace. "
        "Native web tools, when configured, are for public research. "
        "Never inspect private evaluator or adapter files."
    )
    files = FileTools(LocalFileRuntime(workspace), enable_image_reads=options.image_tools)
    shell = await stack.enter_async_context(
        ScrubbedShellRuntime(workspace, log_directory=directory / "processes")
    )
    processes = ProcessTools(shell)
    todos = TodoList()
    tools = (
        files.read_tool(),
        files.edit_tool(),
        files.write_tool(),
        files.grep_tool(),
        files.glob_tool(),
        processes.bash_tool(),
        processes.task_list_tool(),
        processes.task_output_tool(),
        processes.task_stop_tool(),
        todos.tool(),
    )
    if options.web_tools:
        provider = ExaWebProvider(api_key=os.environ["EXA_API_KEY"])
        tools += (web_search_tool(provider), web_fetch_tool(provider))
    if options.profile == "prod":
        tools = production_tools(tools, image_reads=options.image_tools)
        prompt = build_production_instructions(
            skills_root=workspace / ".agents",
            workspace=str(workspace),
            additional_instructions=instructions,
            enable_image_tools=options.image_tools,
        )
    else:
        prompt = build_instructions(skills_root=workspace / ".agents") + "\n\n" + instructions
    return tools, prompt, compaction
