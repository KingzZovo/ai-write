"""Outline model boundary: all delivery uses Registry/cache and tier routing."""
from contextlib import aclosing
from app.services import prompt_registry


class OutlinePromptRunner:
    def __init__(self, project_id=None):
        self.project_id = str(project_id) if project_id else None

    async def generate(self, task_type, messages, **kwargs):
        kwargs.pop('_log_meta', None)
        kwargs.pop('stream', None)
        return await prompt_registry.run_text_prompt(
            task_type, '', None, messages=messages, project_id=self.project_id, **kwargs)

    async def generate_stream(self, task_type, messages, **kwargs):
        kwargs.pop('_log_meta', None)
        kwargs.pop('stream', None)
        async with aclosing(prompt_registry.stream_text_prompt(
            task_type, '', None, messages=messages, project_id=self.project_id, **kwargs
        )) as stream:
            async for chunk in stream:
                yield chunk
