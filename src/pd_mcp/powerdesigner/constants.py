"""PowerDesigner metaclass ids and model-kind metadata.

Values verified from the vendor-supplied constants file
``D:\\powerdesigner\\Ole Automation\\VBScriptConstants.vbs``
("Constants for external VB-Script access to Sybase PowerDesigner 16.5.0")
and cross-checked against the official C# sample
(``pd.CreateModel((int)PdPDM.PdPDM_Classes.cls_Model, "|Diagram=PhysicalDiagram", 0)``).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..errors import InvalidParamsError

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
        # container / capability metadata (see the note above)
        "object_word": "table",
        "column_coll": "Columns",
        "key_coll": "Keys",
        "index_coll": "Indexes",
        "pk_flag_prop": "Primary",         # flag lives on the key object
        "pk_owner_prop": "PrimaryKey",     # back-pointer lives on the table
        "ref_style": "physical",           # ParentTable/ChildTable/Joins/ParentKey
    },
    "CDM": {
        "model_class": PD_CDM_MODEL,
        "diagram_collection": "ConceptualDiagrams",
        "diagram_template": "|Diagram=ConceptualDiagram",
        "object_class": PD_CDM_ENTITY,
        "objects_collection": "Entities",
        "ref_collection": "Relationships",
        "object_word": "entity",
        "column_coll": "Attributes",
        "key_coll": "Identifiers",
        "index_coll": None,
        "pk_flag_prop": None,              # attributes have no Primary flag
        "pk_owner_prop": "PrimaryIdentifier",
        "ref_style": "conceptual",         # Entity1/Entity2 + RoleCardinality
    },
    "LDM": {
        "model_class": PD_LDM_MODEL,
        "diagram_collection": "LogicalDiagrams",
        "diagram_template": "|Diagram=LogicalDiagram",
        "object_class": PD_LDM_ENTITY,
        "objects_collection": "Entities",
        "ref_collection": "Relationships",
        "object_word": "entity",
        "column_coll": "Attributes",
        "key_coll": "Identifiers",
        "index_coll": None,
        "pk_flag_prop": None,
        "pk_owner_prop": "PrimaryIdentifier",
        "ref_style": "conceptual",
    },
}


def kind_meta(kind: str) -> Dict[str, Any]:
    """Container/capability metadata for a model kind (PDM/CDM/LDM)."""
    try:
        return MODEL_KINDS[kind.upper()]
    except KeyError:
        raise InvalidParamsError(
            f"Unsupported model kind '{kind}'; expected one of "
            f"{', '.join(sorted(MODEL_KINDS))}")


def supports_indexes(kind: str) -> bool:
    return kind_meta(kind)["index_coll"] is not None


def uses_conceptual_relationships(kind: str) -> bool:
    return kind_meta(kind)["ref_style"] == "conceptual"


# --- conceptual (CDM/LDM) data types ---------------------------------------
#
# CDM/LDM do not use DBMS column syntax: PD stores its own type vocabulary
# ("Characters(12)", "Variable characters(20)", "Decimal(6,2)", "Timestamp")
# and maps it per target DBMS at generation time.  Passing SQL syntax through
# verbatim yields an "unknown datatype" check error, so common SQL spellings
# are translated here (values live-verified against the .cdm produced by
# PowerDesigner for the campus-canteen course design).
_CONCEPTUAL_TYPE_ALIASES = {
    "int": "Integer", "integer": "Integer", "serial": "Integer",
    "bigint": "Long integer", "long": "Long integer",
    "smallint": "Short integer", "tinyint": "Byte", "byte": "Byte",
    "bit": "Boolean", "bool": "Boolean", "boolean": "Boolean",
    "float": "Float", "real": "Float", "double": "Float",
    "date": "Date", "time": "Time",
    "datetime": "Date & Time", "timestamp": "Timestamp",
    "text": "Text", "longtext": "Text", "clob": "Text", "mediumtext": "Text",
    "blob": "Binary", "binary": "Binary", "varbinary": "Binary",
    "varchar": "Variable characters", "nvarchar": "Variable characters",
    "varchar2": "Variable characters", "char": "Characters",
    "nchar": "Characters", "character": "Characters",
    "decimal": "Decimal", "numeric": "Decimal", "number": "Number",
    "money": "Decimal",
}
_LENGTH_FAMILIES = {"Characters", "Variable characters", "Binary"}
_SCALE_FAMILIES = {"Decimal"}


def conceptual_data_type(base: Any, length: Any = None,
                         precision: Any = None) -> Optional[str]:
    """Render a CDM/LDM attribute data type from SQL-ish input.

    ``base`` may already be a PD type ("Variable characters(20)"), in which
    case it is returned untouched; otherwise common SQL spellings are mapped
    onto the PD vocabulary and the length/scale is folded into the string.
    """
    if base is None or str(base).strip() == "":
        return None
    text = str(base).strip()
    if "(" in text:                       # already a sized PD type
        return text
    family = _CONCEPTUAL_TYPE_ALIASES.get(text.lower())
    if family is None:                    # unknown: keep the author's spelling
        family = text
    if length and family in _LENGTH_FAMILIES:
        return f"{family}({int(length)})"
    if precision and family in _SCALE_FAMILIES:
        n = int(length) if length else 10
        return f"{family}({n},{int(precision)})"
    if length and family in _SCALE_FAMILIES:
        return f"{family}({int(length)})"
    return family
