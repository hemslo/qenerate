from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional, cast

from graphql import (
    FieldNode,
    GraphQLList,
    GraphQLNonNull,
    GraphQLOutputType,
    GraphQLScalarType,
    GraphQLSchema,
    InlineFragmentNode,
    IntrospectionQuery,
    OperationDefinitionNode,
    Visitor,
    TypeInfo,
    TypeInfoVisitor,
    build_client_schema,
    visit,
    parse,
    get_operation_ast,
    validate,
)
from qenerate.core.plugin import Plugin

from qenerate.plugins.pydantic_v1.mapper import (
    graphql_primitive_to_python,
    graphql_field_name_to_python,
)


HEADER = '"""\nTHIS IS AN AUTO-GENERATED FILE. DO NOT MODIFY MANUALLY!\n"""\n'

IMPORTS = (
    "from typing import Optional, Union  # noqa: F401 # pylint: disable=W0611\n"
    "\n"
    "from pydantic import BaseModel, Extra, Field, Json  "
    "# noqa: F401  # pylint: disable=W0611"
)

INDENT = "    "


@dataclass
class ParsedNode:
    parent: Optional[ParsedNode]
    fields: list[ParsedNode]
    parsed_type: ParsedFieldType

    def class_code_string(self) -> str:
        return ""


@dataclass
class ParsedInlineFragmentNode(ParsedNode):
    def class_code_string(self) -> str:
        # Assure not Optional[]
        if not (self.parent and self.parsed_type):
            return ""

        if self.parsed_type.is_primitive:
            return ""

        lines = ["\n\n"]
        lines.append(
            (
                "class "
                f"{self.parsed_type.unwrapped_python_type}"
                f"({self.parent.parsed_type.unwrapped_python_type}):"
            )
        )
        for field in self.fields:
            if isinstance(field, ParsedClassNode):
                lines.append(
                    (
                        f"{INDENT}{field.py_key}: {field.field_type()} = "
                        f'Field(..., alias="{field.gql_key}")'
                    )
                )

        # https://pydantic-docs.helpmanual.io/usage/model_config/#smart-union
        # https://stackoverflow.com/a/69705356/4478420
        lines.append("")
        lines.append(f"{INDENT}class Config:")
        lines.append(f"{INDENT}{INDENT}smart_union = True")
        lines.append(f"{INDENT}{INDENT}extra = Extra.forbid")

        return "\n".join(lines)


@dataclass
class ParsedClassNode(ParsedNode):
    gql_key: str
    py_key: str

    def class_code_string(self) -> str:
        if self.parsed_type.is_primitive:
            return ""

        lines = ["\n\n"]
        lines.append(f"class {self.parsed_type.unwrapped_python_type}(BaseModel):")
        for field in self.fields:
            if isinstance(field, ParsedClassNode):
                lines.append(
                    (
                        f"{INDENT}{field.py_key}: {field.field_type()} = "
                        f'Field(..., alias="{field.gql_key}")'
                    )
                )

        # https://pydantic-docs.helpmanual.io/usage/model_config/#smart-union
        # https://stackoverflow.com/a/69705356/4478420
        lines.append("")
        lines.append(f"{INDENT}class Config:")
        lines.append(f"{INDENT}{INDENT}smart_union = True")
        lines.append(f"{INDENT}{INDENT}extra = Extra.forbid")

        return "\n".join(lines)

    def field_type(self) -> str:
        unions: list[str] = []
        # TODO: sorting does not need to happen on each call
        """
        Pydantic does best-effort matching on Unions.
        Declare most significant type first.
        This, smart_union and disallowing extra fields gives high confidence
        in matching.
        https://pydantic-docs.helpmanual.io/usage/types/#unions
        """
        self.fields.sort(key=lambda a: len(a.fields), reverse=True)
        for field in self.fields:
            if isinstance(field, ParsedInlineFragmentNode):
                unions.append(field.parsed_type.unwrapped_python_type)
        if len(unions) > 0:
            unions.append(self.parsed_type.unwrapped_python_type)
            return self.parsed_type.wrapped_python_type.replace(
                self.parsed_type.unwrapped_python_type, f"Union[{', '.join(unions)}]"
            )
        return self.parsed_type.wrapped_python_type


@dataclass
class ParsedOperationNode(ParsedNode):
    def class_code_string(self) -> str:
        lines = ["\n\n"]
        lines.append(f"class {self.parsed_type.unwrapped_python_type}Query(BaseModel):")
        for field in self.fields:
            if isinstance(field, ParsedClassNode):
                lines.append(
                    (
                        f"{INDENT}{field.py_key}: {field.field_type()} = "
                        f'Field(..., alias="{field.gql_key}")'
                    )
                )

        # https://pydantic-docs.helpmanual.io/usage/model_config/#smart-union
        # https://stackoverflow.com/a/69705356/4478420
        lines.append("")
        lines.append(f"{INDENT}class Config:")
        lines.append(f"{INDENT}{INDENT}smart_union = True")
        lines.append(f"{INDENT}{INDENT}extra = Extra.forbid")

        return "\n".join(lines)


@dataclass
class ParsedFieldType:
    unwrapped_python_type: str
    wrapped_python_type: str
    is_primitive: bool


class FieldToTypeMatcherVisitor(Visitor):
    def __init__(self, schema: GraphQLSchema, type_info: TypeInfo, query: str):
        # These are required for GQL Visitor to do its magic
        Visitor.__init__(self)
        self.schema = schema
        self.type_info = type_info
        self.query = query

        # These are our custom fields
        self.parsed = ParsedNode(
            parent=None,
            fields=[],
            parsed_type=ParsedFieldType(
                unwrapped_python_type="",
                wrapped_python_type="",
                is_primitive=False,
            ),
        )
        self.parent = self.parsed
        self.deduplication_cache: set[str] = set()

    def enter_inline_fragment(self, node: InlineFragmentNode, *_):
        graphql_type = self.type_info.get_type()
        if not graphql_type:
            raise ValueError(f"{node} does not have a graphql type")
        field_type = self._parse_type(graphql_type=graphql_type)
        current = ParsedInlineFragmentNode(
            fields=[],
            parent=self.parent,
            parsed_type=field_type,
        )
        self.parent.fields.append(current)
        self.parent = current

    def leave_inline_fragment(self, *_):
        self.parent = self.parent.parent if self.parent else self.parent

    def enter_operation_definition(self, node: OperationDefinitionNode, *_):
        if not node.name:
            raise ValueError(f"{node} does not have a name defined")
        current = ParsedOperationNode(
            parent=self.parent,
            fields=[],
            parsed_type=ParsedFieldType(
                unwrapped_python_type=node.name.value,
                wrapped_python_type=f"Optional[list[{node.name.value}]]",
                is_primitive=False,
            ),
        )
        self.parent.fields.append(current)
        self.parent = current

    def leave_operation_definition(self, *_):
        self.parent = self.parent.parent if self.parent else self.parent

    def enter_field(self, node: FieldNode, *_):
        graphql_type = self.type_info.get_type()
        if not graphql_type:
            raise ValueError(f"{node} does not have a graphql type")
        field_type = self._parse_type(graphql_type=graphql_type)
        py_key = graphql_field_name_to_python(node.name.value)
        gql_key = node.alias.value if node.alias else node.name.value
        current = ParsedClassNode(
            fields=[],
            parent=self.parent,
            parsed_type=field_type,
            py_key=py_key,
            gql_key=gql_key,
        )

        self.parent.fields.append(current)
        self.parent = current

    def leave_field(self, *_):
        self.parent = self.parent.parent if self.parent else self.parent

    # Custom Functions
    def _parse_type(self, graphql_type: GraphQLOutputType) -> ParsedFieldType:
        is_optional = True
        if isinstance(graphql_type, GraphQLNonNull):
            is_optional = False
            graphql_type = graphql_type.of_type

        is_list = False
        if isinstance(graphql_type, GraphQLList):
            is_list = True
            graphql_type = graphql_type.of_type

        needs_further_unwrapping = isinstance(
            graphql_type, GraphQLNonNull
        ) or isinstance(graphql_type, GraphQLList)
        parsed_of_type = None
        if needs_further_unwrapping:
            parsed_of_type = self._parse_type(graphql_type=graphql_type)

        unwrapped_type = (
            self._to_python_type(graphql_type)
            if not parsed_of_type
            else parsed_of_type.unwrapped_python_type
        )
        wrapped_type = (
            unwrapped_type if not parsed_of_type else parsed_of_type.wrapped_python_type
        )
        is_primitive = (
            isinstance(graphql_type, GraphQLScalarType)
            if not parsed_of_type
            else parsed_of_type.is_primitive
        )

        if is_optional and is_list:
            wrapped_type = f"Optional[list[{wrapped_type}]]"
        elif is_optional:
            wrapped_type = f"Optional[{wrapped_type}]"
        elif is_list:
            wrapped_type = f"list[{wrapped_type}]"

        return ParsedFieldType(
            unwrapped_python_type=unwrapped_type,
            wrapped_python_type=wrapped_type,
            is_primitive=is_primitive,
        )

    def _to_python_type(self, graphql_type: GraphQLOutputType) -> str:
        if isinstance(graphql_type, GraphQLScalarType):
            return graphql_primitive_to_python(graphql_type=graphql_type)
        else:
            cur = self.parent
            class_name = str(graphql_type).replace("_", "")
            class_name = f"{class_name[:-2]}V{class_name[-1]}"
            while cur and cur.parent and class_name in self.deduplication_cache:
                class_name = f"{cur.parsed_type.unwrapped_python_type}_{class_name}"
                cur = cur.parent

            self.deduplication_cache.add(class_name)
            return class_name


class AnonymousQueryError(Exception):
    def __init__(self):
        super().__init__("All queries must be named")


class InvalidQueryError(Exception):
    def __init__(self, errors):
        self.errors = errors
        message = "\n".join(str(err) for err in errors)
        super().__init__(message)


class QueryParser:
    @staticmethod
    def parse(query: str, schema: GraphQLSchema) -> ParsedNode:
        document_ast = parse(query)
        operation = get_operation_ast(document_ast)

        if operation and not operation.name:
            raise AnonymousQueryError()

        errors = validate(schema, document_ast)
        if errors:
            raise InvalidQueryError(errors)

        type_info = TypeInfo(schema)
        visitor = FieldToTypeMatcherVisitor(schema, type_info, query)
        visit(document_ast, TypeInfoVisitor(type_info, visitor))
        return visitor.parsed


class PydanticV1Plugin(Plugin):
    def _traverse(self, node: ParsedNode) -> str:
        """
        Pydantic doesnt play well with from __future__ import annotations
        --> order of class declaration is important:
        - post-order for non-inline fragment nodes, i.e., non-interface nodes
        - pre-order for nodes that implement an interface
        """
        result = ""
        for child in node.fields:
            if not isinstance(child, ParsedInlineFragmentNode):
                result = f"{result}{self._traverse(child)}"

        result = f"{result}{node.class_code_string()}"

        for child in node.fields:
            if isinstance(child, ParsedInlineFragmentNode):
                result = f"{result}{self._traverse(child)}"
        return result

    def generate(self, query: str, raw_schema: dict[Any, Any]) -> str:
        result = HEADER + IMPORTS
        schema = build_client_schema(cast(IntrospectionQuery, raw_schema))
        parser = QueryParser()
        ast = parser.parse(query=query, schema=schema)
        result += self._traverse(ast)
        result += "\n"
        return result