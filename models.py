"""Data models for PDF processing responses."""

from typing import Dict, List, Any, Optional


class BoundingBox:
    def __init__(self, x0: float, y0: float, x1: float, y1: float):
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1

    def to_dict(self) -> Dict[str, float]:
        return {
            "x0": float(self.x0),
            "y0": float(self.y0),
            "x1": float(self.x1),
            "y1": float(self.y1),
        }


class WordBoundingBox:
    def __init__(
        self,
        page: int,
        text: str,
        bbox: Dict[str, float],
        absolute_bbox: Dict[str, float],
        page_dimensions: Dict[str, float],
        block_no: int,
        line_no: int,
        word_no: int,
    ):
        self.page = page
        self.text = text
        self.bbox = bbox
        self.absolute_bbox = absolute_bbox
        self.page_dimensions = page_dimensions
        self.block_no = block_no
        self.line_no = line_no
        self.word_no = word_no

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": int(self.page),
            "text": str(self.text),
            "bbox": self.bbox,
            "absolute_bbox": self.absolute_bbox,
            "page_dimensions": self.page_dimensions,
            "block_no": int(self.block_no),
            "line_no": int(self.line_no),
            "word_no": int(self.word_no),
        }


class ImageData:
    def __init__(
        self,
        number: int,
        bbox: Dict[str, float],
        transform: List[float],
        width: int,
        height: int,
        colorspace: int,
        cs_name: str,
        xres: int,
        yres: int,
        bpc: int,
        size: int,
    ):
        self.number = number
        self.bbox = bbox
        self.transform = transform
        self.width = width
        self.height = height
        self.colorspace = colorspace
        self.cs_name = cs_name
        self.xres = xres
        self.yres = yres
        self.bpc = bpc
        self.size = size

    def to_dict(self) -> Dict[str, Any]:
        return {
            "number": int(self.number),
            "bbox": self.bbox,
            "transform": [float(x) for x in self.transform],
            "width": int(self.width),
            "height": int(self.height),
            "colorspace": int(self.colorspace),
            "cs_name": str(self.cs_name),
            "xres": int(self.xres),
            "yres": int(self.yres),
            "bpc": int(self.bpc),
            "size": int(self.size),
        }


class DocumentInfo:
    def __init__(
        self,
        page_count: int,
        file_size: int,
        title: str,
        author: str,
        subject: str,
        creator: str,
    ):
        self.page_count = page_count
        self.file_size = file_size
        self.title = title
        self.author = author
        self.subject = subject
        self.creator = creator

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_count": int(self.page_count),
            "file_size": int(self.file_size),
            "title": str(self.title or ""),
            "author": str(self.author or ""),
            "subject": str(self.subject or ""),
            "creator": str(self.creator or ""),
        }


class PageData:
    def __init__(
        self,
        metadata: Dict[str, Any],
        toc_items: List[Any],
        tables: List[Any],
        images: List[ImageData],
        graphics: List[Any],
        text: str,
        words: List[Any],
    ):
        self.metadata = metadata
        self.toc_items = toc_items
        self.tables = tables
        self.images = images
        self.graphics = graphics
        self.text = text
        self.words = words

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata,
            "toc_items": self.toc_items,
            "tables": self.tables,
            "images": [img.to_dict() for img in self.images],
            "graphics": self.graphics,
            "text": str(self.text),
            "words": self.words,
        }


class PDFProcessingResponse:
    def __init__(
        self,
        success: bool,
        document_info: DocumentInfo,
        word_bounding_boxes: List[WordBoundingBox],
        word_count: int,
        structured_data: List[PageData],
        error: Optional[str] = None,
        error_type: Optional[str] = None,
    ):
        self.success = success
        self.document_info = document_info
        self.word_bounding_boxes = word_bounding_boxes
        self.word_count = word_count
        self.structured_data = structured_data
        self.error = error
        self.error_type = error_type

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "success": bool(self.success),
            "document_info": self.document_info.to_dict(),
            "word_bounding_boxes": [wb.to_dict() for wb in self.word_bounding_boxes],
            "word_count": int(self.word_count),
            "structured_data": [pd.to_dict() for pd in self.structured_data],
        }

        if self.error:
            result["error"] = str(self.error)
        if self.error_type:
            result["error_type"] = str(self.error_type)

        return result
