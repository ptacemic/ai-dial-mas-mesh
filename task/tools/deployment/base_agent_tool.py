import json
from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Any

from aidial_client import AsyncDial
from aidial_sdk.chat_completion import Message, Role, CustomContent, Stage, Attachment
from pydantic import StrictStr

from task.tools.base_tool import BaseTool
from task.tools.models import ToolCallParams
from task.utils.constants import TOOL_CALL_HISTORY_KEY
from task.utils.stage import StageProcessor


def _clean_attachment_for_api(att: Attachment) -> dict[str, Any]:
    """Clean attachment to only include fields accepted by the API."""
    att_dict = {}
    # Use url or reference_url (whichever is available)
    if att.url:
        att_dict["url"] = str(att.url)
    elif att.reference_url:
        att_dict["url"] = str(att.reference_url)
    
    if att.type:
        att_dict["type"] = str(att.type)
    if att.title:
        att_dict["title"] = str(att.title)
    
    return att_dict


class BaseAgentTool(BaseTool, ABC):

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    @property
    @abstractmethod
    def deployment_name(self) -> str:
        pass

    async def _execute(self, tool_call_params: ToolCallParams) -> str | Message:
        # 1. Get prompt and propagate_history from tool call
        arguments = json.loads(tool_call_params.tool_call.function.arguments)
        prompt = arguments.get("prompt", "")
        propagate_history = arguments.get("propagate_history", False)
        
        # 2. Use AsyncDial to call the agent with streaming
        client = AsyncDial(
            base_url=self.endpoint,
            api_key=tool_call_params.api_key,
            api_version='2025-01-01-preview'
        )
        
        # Prepare messages with history
        messages = self._prepare_messages(tool_call_params)
        
        # Call agent with streaming
        chunks = await client.chat.completions.create(
            messages=messages,
            deployment_name=self.deployment_name,
            stream=True,
            extra_headers={
                "x-conversation-id": tool_call_params.conversation_id
            }
        )
        
        # 3. Prepare variables
        content = ""
        custom_content = CustomContent(attachments=[])
        stages_map: dict[int, Stage] = {}
        
        # 4. Iterate through chunks
        async for chunk in chunks:
            if chunk.choices and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                
                # Stream content to stage
                if delta and delta.content:
                    if tool_call_params.stage:
                        tool_call_params.stage.append_content(delta.content)
                    content += delta.content
                
                # Handle custom_content (check if it exists on chunk.choices[0], not on delta)
                choice_obj = chunk.choices[0]
                if hasattr(choice_obj, 'custom_content') and choice_obj.custom_content:
                    response_custom_content = choice_obj.custom_content
                    
                    # Set state from response
                    if response_custom_content.state:
                        custom_content.state = response_custom_content.state
                    
                    # Propagate attachments
                    if response_custom_content.attachments:
                        for attachment in response_custom_content.attachments:
                            custom_content.attachments.append(attachment)
                            # Propagate to choice
                            tool_call_params.choice.add_attachment(attachment)
                    
                    # Stages propagation
                    if response_custom_content.state:
                        state_dict = response_custom_content.state
                        if isinstance(state_dict, dict) and "stages" in state_dict:
                            stages = state_dict["stages"]
                            if isinstance(stages, list):
                                for stage_data in stages:
                                    if isinstance(stage_data, dict):
                                        stage_index = stage_data.get("index")
                                        stage_name = stage_data.get("name")
                                        stage_content = stage_data.get("content", "")
                                        stage_status = stage_data.get("status")
                                        stage_attachments = stage_data.get("attachments", [])
                                        
                                        if stage_index is not None:
                                            # Get or create stage
                                            if stage_index not in stages_map:
                                                propagated_stage = tool_call_params.choice.create_stage(stage_name)
                                                propagated_stage.open()
                                                stages_map[stage_index] = propagated_stage
                                            else:
                                                propagated_stage = stages_map[stage_index]
                                            
                                            # Propagate content
                                            if stage_content and stage_content != propagated_stage.content:
                                                propagated_stage.append_content(stage_content)
                                            
                                            # Propagate attachments
                                            for attachment_data in stage_attachments:
                                                if isinstance(attachment_data, dict):
                                                    attachment = Attachment(**attachment_data)
                                                    propagated_stage.add_attachment(attachment)
                                            
                                            # Close stage if completed
                                            if stage_status == "completed":
                                                StageProcessor.close_stage_safely(propagated_stage)
        
        # 5. Ensure all stages are closed
        for stage in stages_map.values():
            StageProcessor.close_stage_safely(stage)
        
        # 6. Return Tool message
        return Message(
            role=Role.TOOL,
            name=StrictStr(tool_call_params.tool_call.function.name),
            tool_call_id=StrictStr(tool_call_params.tool_call.id),
            content=content,
            custom_content=custom_content
        )

    def _prepare_messages(self, tool_call_params: ToolCallParams) -> list[dict[str, Any]]:
        # 1. Get prompt and propagate_history from tool call
        arguments = json.loads(tool_call_params.tool_call.function.arguments)
        prompt = arguments.get("prompt", "")
        propagate_history = arguments.get("propagate_history", False)
        
        # 2. Prepare empty messages array
        messages: list[dict[str, Any]] = []
        
        # 3. Collect proper history if propagate_history is True
        if propagate_history:
            i = 0
            while i < len(tool_call_params.messages):
                message = tool_call_params.messages[i]
                
                # If assistant message with state containing history for this agent
                if message.role == Role.ASSISTANT and message.custom_content and message.custom_content.state:
                    state = message.custom_content.state
                    if isinstance(state, dict) and self.name in state:
                        # Get the history for this agent
                        agent_state = state[self.name]
                        if isinstance(agent_state, dict) and TOOL_CALL_HISTORY_KEY in agent_state:
                            tool_call_history = agent_state[TOOL_CALL_HISTORY_KEY]
                            
                            # Find the user message before this assistant message
                            if i > 0 and tool_call_params.messages[i - 1].role == Role.USER:
                                user_msg = tool_call_params.messages[i - 1]
                                # Add user message
                                user_msg_dict = {
                                    "role": Role.USER.value,
                                    "content": user_msg.content or ""
                                }
                                if user_msg.custom_content and user_msg.custom_content.attachments:
                                    # Only include allowed fields for attachments (url, type, title)
                                    # Attachments must be in custom_content, not at top level
                                    attachments_list = []
                                    for att in user_msg.custom_content.attachments:
                                        att_dict = _clean_attachment_for_api(att)
                                        # Only add if we have at least a URL
                                        if "url" in att_dict:
                                            attachments_list.append(att_dict)
                                    
                                    if attachments_list:
                                        user_msg_dict["custom_content"] = {
                                            "attachments": attachments_list
                                        }
                                messages.append(user_msg_dict)
                            
                            # Add tool messages and assistant message from history
                            for history_msg in tool_call_history:
                                if isinstance(history_msg, dict):
                                    if history_msg.get("role") == Role.TOOL.value:
                                        messages.append({
                                            "role": Role.TOOL.value,
                                            "content": history_msg.get("content", ""),
                                            "tool_call_id": history_msg.get("tool_call_id")
                                        })
                            
                            # Add assistant message with refactored state
                            assistant_msg_copy = deepcopy(message)
                            # Replace state with agent-specific state
                            if assistant_msg_copy.custom_content:
                                assistant_msg_copy.custom_content.state = agent_state
                                # Clean up attachments in custom_content to only include allowed fields
                                if assistant_msg_copy.custom_content.attachments:
                                    cleaned_attachments = []
                                    for att in assistant_msg_copy.custom_content.attachments:
                                        att_dict = {}
                                        if att.url:
                                            att_dict["url"] = str(att.url)
                                        elif att.reference_url:
                                            att_dict["url"] = str(att.reference_url)
                                        if att.type:
                                            att_dict["type"] = str(att.type)
                                        if att.title:
                                            att_dict["title"] = str(att.title)
                                        if "url" in att_dict:
                                            cleaned_attachments.append(att_dict)
                                    # Replace with cleaned attachments
                                    assistant_msg_copy.custom_content.attachments = [
                                        Attachment(**att_dict) for att_dict in cleaned_attachments
                                    ]
                            
                            assistant_msg_dict = assistant_msg_copy.dict(exclude_none=True)
                            # Clean up attachments in custom_content if they exist
                            if "custom_content" in assistant_msg_dict and assistant_msg_dict["custom_content"]:
                                if "attachments" in assistant_msg_dict["custom_content"]:
                                    # Ensure attachments only have allowed fields
                                    cleaned_attachments = []
                                    for att_data in assistant_msg_dict["custom_content"]["attachments"]:
                                        if isinstance(att_data, dict):
                                            # Reconstruct Attachment object to use our cleaning function
                                            try:
                                                att = Attachment(**att_data)
                                                cleaned_att = _clean_attachment_for_api(att)
                                                if "url" in cleaned_att:
                                                    cleaned_attachments.append(cleaned_att)
                                            except Exception:
                                                # If we can't parse it, try to clean it manually
                                                cleaned_att = {}
                                                if "url" in att_data:
                                                    cleaned_att["url"] = att_data["url"]
                                                elif "reference_url" in att_data:
                                                    cleaned_att["url"] = att_data["reference_url"]
                                                if "type" in att_data:
                                                    cleaned_att["type"] = att_data["type"]
                                                if "title" in att_data:
                                                    cleaned_att["title"] = att_data["title"]
                                                if "url" in cleaned_att:
                                                    cleaned_attachments.append(cleaned_att)
                                    assistant_msg_dict["custom_content"]["attachments"] = cleaned_attachments
                            
                            messages.append(assistant_msg_dict)
                
                i += 1
        
        # 4. Add the user message with prompt
        user_message: dict[str, Any] = {
            "role": Role.USER.value,
            "content": prompt
        }
        
        # Add custom_content if present in the last user message
        if tool_call_params.messages and tool_call_params.messages[-1].role == Role.USER:
            last_user_msg = tool_call_params.messages[-1]
            if last_user_msg.custom_content and last_user_msg.custom_content.attachments:
                # Only include allowed fields for attachments (url, type, title)
                # Attachments must be in custom_content, not at top level
                attachments_list = []
                for att in last_user_msg.custom_content.attachments:
                    att_dict = _clean_attachment_for_api(att)
                    # Only add if we have at least a URL
                    if "url" in att_dict:
                        attachments_list.append(att_dict)
                
                if attachments_list:
                    user_message["custom_content"] = {
                        "attachments": attachments_list
                    }
        
        messages.append(user_message)
        
        return messages