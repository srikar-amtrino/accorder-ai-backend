from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


class SummaryToolRequest(BaseModel):
    """Request Schema for the Summary Tool."""

    session_id: Optional[str] = Field(..., description="Session Id for the data lookup in the database.")


class SummaryToolResponse(BaseModel):
    """Response Schema for the summary tool."""

    summary: str = Field(..., description="Summary of the given document.")


class KeyInforamtionToolRequest(BaseModel):
    """Request Schema for the Key Information Tool."""

    session_id: str = Field(..., description="Session Id for the data lookup in the database.")


class KeyInformationData(BaseModel):
    """Data Types Schema for the Key Information Data."""

    value: Optional[str] = Field(..., description="Value of the particular key field.")
    information: Optional[str] = Field(..., description="Detailed description of the key information(value) extracted.")


class KeyInfomationPartiesSchema(BaseModel):
    """Response Schema for the parties involved in the document."""

    name: str = Field(..., description="Full legal name exactly as stated, including any subsidiary/affiliate qualifier.")
    role: str = Field(..., description="Role label exactly as used in the contract.")
    address: Optional[str] = Field(None, description="Address from the signature block or notice section. Null if not stated.")
    email: Optional[str] = Field(None, description="Email from the signature block or notice section. Null if not stated.")


class KeyInformationToolResponse(BaseModel):
    """Response Schema for the Key Information Tool."""

    effective_date: KeyInformationData = Field(..., description="The date that the contract becomes legally effective.")
    expiration_date: KeyInformationData = Field(..., description="The date the contract expires or terminates.")
    contract_value: KeyInformationData = Field(..., description="Total monetary value of the contract including currency.")
    duration: KeyInformationData = Field(..., description="Total contract term resolved to both years and months.")
    net_term: KeyInformationData = Field(..., description="Payment net term exactly as stated.")
    contract_type: KeyInformationData = Field(..., description="Official title of the agreement as stated in the document.")
    governing_law: KeyInformationData = Field(..., description="Jurisdiction and venue/forum clause exactly as stated.")
    notes: Optional[str] = Field(..., description="Any other additional information; Free-text observations about ambiguities, blank placeholders, conditional dates etc.")
    parties: List[KeyInfomationPartiesSchema] = Field(..., description="All named contracting parties.")


class NDAGenerationRequest(BaseModel):
    """Request schema for NDA generation tool."""

    nda_description: str = Field(..., description="User-provided description and requirements for the NDA.")
    # step: str = Field("headings", description="Step of generation: headings, add_content, refine, final")
    # prior_draft: Optional[str] = Field(None, description="Previous output from an earlier step (if applicable).")
    headings: Optional[str] = Field(None, description="selected NDA heading.")


class NDAGenerationResponse(BaseModel):
    """Response schema for NDA generation tool."""

    generated_text: str = Field(..., description="Generated NDA text for the requested step.")


class ToolResponse(BaseModel):
    """Response Schema for the tool responses."""

    tool_id: Union[str, None] = Field(None, description="Unique id for the tool.")
    status: bool = Field(..., description="Response status of the tool.")
    response: Union[Dict[str, Any], BaseModel] = Field(..., description="Response content of the tool.")
    metadata: Dict[str, Any] = Field(..., description="Tool Response Metadata.")
    response_time: str = Field(..., description="Time taken for the response.")
