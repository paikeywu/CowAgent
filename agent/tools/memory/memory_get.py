"""
Memory get tool

Allows agents to read specific sections from memory files
"""

import os

from agent.tools.base_tool import BaseTool
from agent.tools.utils.credentials import DENIED_MESSAGE, is_credential_path
from agent.memory.visibility import RELEASED_PLACEHOLDER, SEALED_PLACEHOLDER


class MemoryGetTool(BaseTool):
    """Tool for reading memory file contents"""
    
    name: str = "memory_get"
    description: str = (
        "Read specific content from memory files. "
        "Use this to get full context from a memory file or specific line range."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the memory file (e.g. 'MEMORY.md', 'memory/2026-01-01.md')"
            },
            "start_line": {
                "type": "integer",
                "description": "Starting line number (optional, default: 1)",
                "default": 1
            },
            "num_lines": {
                "type": "integer",
                "description": "Number of lines to read (optional, reads all if not specified)"
            }
        },
        "required": ["path"]
    }
    
    def __init__(self, memory_manager):
        """
        Initialize memory get tool
        
        Args:
            memory_manager: MemoryManager instance
        """
        super().__init__()
        self.memory_manager = memory_manager

        from config import conf
        if conf().get("knowledge", True):
            self.description = (
                "Read specific content from memory or knowledge files. "
                "Use this to get full context from a memory file, knowledge page, or specific line range."
            )
            self.params = {**self.params}
            self.params["properties"] = {**self.params["properties"]}
            self.params["properties"]["path"] = {
                "type": "string",
                "description": "Relative path to the memory or knowledge file (e.g. 'MEMORY.md', 'memory/2026-01-01.md', 'knowledge/concepts/moe.md')"
            }
    
    def execute(self, args: dict):
        """
        Execute memory file read
        
        Args:
            args: Dictionary with path, start_line, num_lines
            
        Returns:
            ToolResult with file content
        """
        from agent.tools.base_tool import ToolResult
        
        path = args.get("path")
        start_line = args.get("start_line", 1)
        num_lines = args.get("num_lines")
        
        if not path:
            return ToolResult.fail("Error: path parameter is required")
        if num_lines is not None and num_lines <= 0:
            return ToolResult.fail(f"Error: num_lines must be a positive number of lines (got {num_lines})")
        
        try:
            workspace_dir = self.memory_manager.config.get_workspace()
            
            # Auto-prepend memory/ if not present and not absolute path
            # Exceptions: MEMORY.md in root, knowledge/ files at workspace root
            if not path.startswith('memory/') and not path.startswith('knowledge/') and not path.startswith('/') and path != 'MEMORY.md':
                path = f'memory/{path}'
            
            # Roots this path may legitimately resolve under. A "knowledge/"
            # path goes through state_dir, which sends an Agent with no private
            # copy of its own to the shared root — the same fallback sync()
            # indexes through. Resolving it under the workspace alone means the
            # "knowledge/..." key memory_search just returned cannot be opened
            # here, which is how such an Agent used to lose every knowledge
            # page it had just been told about (#3175 follow-up).
            allowed_roots = [workspace_dir]
            knowledge_root = None
            if path.startswith('knowledge/'):
                from common import state_dir
                knowledge_root = state_dir.knowledge_dir(base=workspace_dir)
                file_path = (knowledge_root / path[len('knowledge/'):]).resolve()
                allowed_roots.append(knowledge_root)
            else:
                file_path = (workspace_dir / path).resolve()

            # Use os.path.realpath + os.sep for cross-platform path validation.
            # str(Path).startswith(str + '/') fails on Windows where Path uses
            # backslashes — see MemoryService._resolve_path for the same pattern.
            # Traversal out of a root ("knowledge/../../etc/passwd") still fails
            # here, because the check runs on the resolved path.
            def _contained(real_path: str, root) -> bool:
                real_root = os.path.realpath(str(root))
                return real_path == real_root or real_path.startswith(real_root + os.sep)

            real_file = os.path.realpath(str(file_path))
            if not any(_contained(real_file, root) for root in allowed_roots):
                return ToolResult.fail("Error: Access denied: path outside workspace")
            
            if is_credential_path(str(file_path)):
                return ToolResult.fail(DENIED_MESSAGE)

            if not file_path.exists():
                return ToolResult.fail(f"Error: File not found: {path}")

            if not file_path.is_file():
                return ToolResult.fail(f"Error: {path} is not a regular file")

            # P0-13 L-1: this tool IS a read exit, and it used to be the
            # one that leaked — it read the file straight off the disk,
            # bypassing the retrieval predicate, so a released body came
            # back in full. Every read exit now asks the SAME judgement
            # (agent.memory.visibility / the manager's release state).
            #
            # P0-13 L-3: if the truth layer cannot be resolved, the read
            # FAILS. An unknown release state must not serve content.
            key = self._visibility_key(file_path, workspace_dir,
                                       knowledge_root=knowledge_root)
            decide = getattr(self.memory_manager, "read_decision", None)
            if decide is not None:
                try:
                    decision, _reason = decide(key)
                except Exception as e:
                    return ToolResult.fail(
                        f"Error: the release state could not be resolved "
                        f"({e}); refusing to serve the file (fail closed)."
                    )
                if decision == "released":
                    return ToolResult.fail(RELEASED_PLACEHOLDER)
                if decision == "sealed":
                    return ToolResult.fail(SEALED_PLACEHOLDER)
            # A manager with no truth layer at all (a stub) has nothing that
            # could be released, so there is nothing to judge.

            content = file_path.read_text(encoding='utf-8')
            lines = content.split('\n')
            
            # Handle line range
            if start_line < 1:
                start_line = 1
            
            start_idx = start_line - 1
            
            if num_lines:
                end_idx = start_idx + num_lines
                selected_lines = lines[start_idx:end_idx]
            else:
                selected_lines = lines[start_idx:]
            
            result = '\n'.join(selected_lines)
            
            # Add metadata
            total_lines = len(lines)
            shown_lines = len(selected_lines)
            
            output = [
                f"File: {path}",
                f"Lines: {start_line}-{start_line + shown_lines - 1} (total: {total_lines})",
                "",
                result
            ]
            
            return ToolResult.success('\n'.join(output))
            
        except Exception as e:
            return ToolResult.fail(f"Error reading memory file: {str(e)}")

    @staticmethod
    def _visibility_key(file_path, workspace_dir, knowledge_root=None) -> str:
        """The index key of a file — the key the release state speaks in.

        The release state is keyed by the same workspace-relative POSIX
        strings ``chunks.path`` carries, so the read exit has to speak the
        same key. A knowledge file lives under the shared knowledge root
        and is keyed ``knowledge/<relative>``, exactly as the scan keys it.
        """
        try:
            if knowledge_root is not None:
                rel = file_path.relative_to(knowledge_root).as_posix()
                return f"knowledge/{rel}"
            return file_path.relative_to(workspace_dir).as_posix()
        except ValueError:
            return file_path.as_posix()
