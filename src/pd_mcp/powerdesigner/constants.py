"""PowerDesigner metaclass ids and model-kind metadata.

Values verified from the vendor-supplied constants file
``D:\\powerdesigner\\Ole Automation\\VBScriptConstants.vbs``
("Constants for external VB-Script access to Sybase PowerDesigner 16.5.0")
and cross-checked against the official C# sample
(``pd.CreateModel((int)PdPDM.PdPDM_Classes.cls_Model, "|Diagram=PhysicalDiagram", 0)``).
"""

from __future__ import annotations

# --- metaclass ids (VBScriptConstants.vbs) ---------------------------------
PD_PDM_MODEL = -840675807        # Physical Data Model
PD_PDM_PACKAGE = -840675806
PD_PDM_PHYSICAL_DIAGRAM = -840675808
PD_PDM_TABLE = 940288576
PD_PDM_COLUMN = 940288577
PD_PDM_INDEX = 940288578
PD_PDM_INDEX_COLUMN = 940288579
PD_PDM_KEY = 940288580
PD_PDM_REFERENCE = 1852601152
PD_PDM_REFERENCE_JOIN = 1852601153
PD_PDM_DOMAIN = 1269429830       # PdPDM_PhysicalDomain
PD_PDM_DEFAULT = 1174907869      # PdPDM_PhysicalDefault

PD_CDM_MODEL = 509178224         # Conceptual Data Model
PD_CDM_PACKAGE = 1503953
PD_CDM_CONCEPTUAL_DIAGRAM = 509178243
PD_CDM_ENTITY = 509178226
PD_CDM_ENTITY_ATTRIBUTE = 509178228
PD_CDM_DATA_ITEM = 509178225
PD_CDM_IDENTIFIER = 509178229
PD_CDM_RELATIONSHIP = 509178231
PD_CDM_INHERITANCE = 509178232
PD_CDM_DOMAIN = 509178230

PD_LDM_MODEL = 1598421368        # Logical Data Model
PD_LDM_PACKAGE = -1242614161
PD_LDM_LOGICAL_DIAGRAM = 1283995252
PD_LDM_ENTITY = -664074415
PD_LDM_ENTITY_ATTRIBUTE = -513212376
PD_LDM_IDENTIFIER = 67637410
PD_LDM_RELATIONSHIP = -621676637
PD_LDM_RELATIONSHIP_JOIN = -341206044
PD_LDM_DOMAIN = 1835509509

# --- per-kind object class ids used by CreateObject/CreateNew --------------
PDM_CLASSES = {
    "table": PD_PDM_TABLE,
    "column": PD_PDM_COLUMN,
    "key": PD_PDM_KEY,
    "index": PD_PDM_INDEX,
    "index_column": PD_PDM_INDEX_COLUMN,
    "reference": PD_PDM_REFERENCE,
    "reference_join": PD_PDM_REFERENCE_JOIN,
    "domain": PD_PDM_DOMAIN,
}

CDM_CLASSES = {
    "table": PD_CDM_ENTITY,              # CDM entity, stored via the same interface
    "column": PD_CDM_ENTITY_ATTRIBUTE,
    "key": PD_CDM_IDENTIFIER,
    "reference": PD_CDM_RELATIONSHIP,
    "domain": PD_CDM_DOMAIN,
}

LDM_CLASSES = {
    "table": PD_LDM_ENTITY,
    "column": PD_LDM_ENTITY_ATTRIBUTE,
    "key": PD_LDM_IDENTIFIER,
    "reference": PD_LDM_RELATIONSHIP,
    "domain": PD_LDM_DOMAIN,
}

MODEL_KINDS = {
    "PDM": {
        "model_class": PD_PDM_MODEL,
        "diagram_collection": "PhysicalDiagrams",
        "diagram_template": "|Diagram=PhysicalDiagram",
        "object_class": PD_PDM_TABLE,
        "objects_collection": "Tables",
        "ref_collection": "References",
    },
    "CDM": {
        "model_class": PD_CDM_MODEL,
        "diagram_collection": "ConceptualDiagrams",
        "diagram_template": "|Diagram=ConceptualDiagram",
        "object_class": PD_CDM_ENTITY,
        "objects_collection": "Entities",
        "ref_collection": "Relationships",
    },
    "LDM": {
        "model_class": PD_LDM_MODEL,
        "diagram_collection": "LogicalDiagrams",
        "diagram_template": "|Diagram=LogicalDiagram",
        "object_class": PD_LDM_ENTITY,
        "objects_collection": "Entities",
        "ref_collection": "Relationships",
    },
}
