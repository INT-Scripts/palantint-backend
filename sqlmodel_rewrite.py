import re

with open("app/db/models.py", "r") as f:
    content = f.read()

# Replace imports
content = content.replace("from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text", "from sqlalchemy import Boolean, DateTime, String, Text, Column")
content = content.replace("from sqlalchemy.dialects.postgresql import UUID", "from sqlalchemy.dialects.postgresql import UUID as PGUUID")
content = content.replace("from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship", "from sqlmodel import SQLModel, Field, Relationship\nfrom sqlalchemy import Column")

# Replace Base inheritance
content = content.replace("class Base(DeclarativeBase):\n    pass\n\n", "")
content = content.replace("(Base)", "(SQLModel, table=True)")

# Replace simple types
content = re.sub(r'Mapped\[uuid.UUID\] = mapped_column\(\n?\s*UUID\([^\)]+\),\s*primary_key=True,\s*default=uuid\.uuid4\n?\s*\)', r'uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)', content)
content = re.sub(r'Mapped\[uuid.UUID\] = mapped_column\(\n?\s*UUID\([^\)]+\),\s*primary_key=True\n?\s*\)', r'uuid.UUID = Field(primary_key=True)', content)

# Mapped[type] = mapped_column(...)
def repl_mapped(match):
    typ = match.group(1)
    args = match.group(2)
    # Convert kwargs to Field kwargs
    args = args.replace("unique=True", "unique=True")
    args = args.replace("index=True", "index=True")
    args = args.replace("nullable=True", "nullable=True")
    
    if "ForeignKey" in args:
        # Field(foreign_key="table.id")
        fk_match = re.search(r'ForeignKey\("([^"]+)"\)', args)
        if fk_match:
            fk = fk_match.group(1)
            args = re.sub(r'ForeignKey\("[^"]+"\),?\s*', f'foreign_key="{fk}", ', args)

    if typ.startswith("Optional["):
        args += ", nullable=True"
        
    if "DateTime" in args and "default=datetime.utcnow" in args:
        args = args.replace("DateTime, default=datetime.utcnow", "default_factory=datetime.utcnow")
    if "DateTime" in args and "onupdate=datetime.utcnow" in args:
        args = args.replace("onupdate=datetime.utcnow", 'sa_column_kwargs={"onupdate": datetime.utcnow}')
    
    # Strip lingering types from SA
    args = re.sub(r'String,?\s*', '', args)
    args = re.sub(r'Text,?\s*', 'sa_column=Column(Text), ', args)
    args = re.sub(r'Boolean,?\s*', '', args)
    args = re.sub(r'DateTime,?\s*', '', args)
    
    return f"{typ} = Field({args})"

content = re.sub(r'Mapped\[([^\]]+)\] = mapped_column\((.*?)\)', repl_mapped, content, flags=re.DOTALL)

# Mapped[List[...]] = relationship(...)
def repl_rel(match):
    typ = match.group(1)
    args = match.group(2)
    args = args.replace("back_populates=", "back_populates=")
    if "cascade=" in args or "foreign_keys=" in args:
        # Wrap in sa_relationship_kwargs
        sa_kwargs = []
        if "cascade=" in args:
            casc_match = re.search(r'cascade="([^"]+)"', args)
            sa_kwargs.append(f'"cascade": "{casc_match.group(1)}"')
            args = re.sub(r'cascade="[^"]+",?\s*', '', args)
        if "foreign_keys=" in args:
            fk_match = re.search(r'foreign_keys=(\[?[^,\]]+\]?)', args)
            if fk_match:
                sa_kwargs.append(f'"foreign_keys": "{fk_match.group(1)}"')
                args = re.sub(r'foreign_keys=\[?[^,\]]+\]?,?\s*', '', args)
            elif 'foreign_keys="[' in args:
                fk_match = re.search(r'foreign_keys="\[([^\]]+)\]"', args)
                if fk_match:
                    sa_kwargs.append(f'"foreign_keys": "[{fk_match.group(1)}]"')
                    args = re.sub(r'foreign_keys="\[[^\]]+\]",?\s*', '', args)
        if sa_kwargs:
            args += f', sa_relationship_kwargs={{{", ".join(sa_kwargs)}}}'
    return f"list[{typ}] = Relationship({args})"

content = re.sub(r'Mapped\[List\["([^"]+)"\]\] = relationship\((.*?)\)', repl_rel, content, flags=re.DOTALL)
content = re.sub(r'Mapped\["([^"]+)"\] = relationship\((.*?)\)', r'\1 = Relationship(\2)', content, flags=re.DOTALL)
content = re.sub(r'Mapped\[Optional\["([^"]+)"\]\] = relationship\((.*?)\)', r'Optional[\1] = Relationship(\2)', content, flags=re.DOTALL)

with open("app/db/models.py", "w") as f:
    f.write(content)
