"""ComfyUI-Universal-Image-Mentions

Model-agnostic @image mention routing for ComfyUI.
No model, GPU, VRAM, LLM, or third-party custom-node dependency.
"""

from .universal_image_mentions import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

# Optional frontend enhancement. If a ComfyUI fork does not support WEB_DIRECTORY,
# the Python nodes still work normally; only @ autocomplete is unavailable.
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
__version__ = "4.2.1"
