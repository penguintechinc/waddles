#!/usr/bin/env python3
"""
Add standardized healthcheck to all Dockerfiles in the repository.
"""
import os
import re
from pathlib import Path


def find_dockerfiles():
    """Find all Dockerfile paths in the repository."""
    dockerfiles = []
    for root, dirs, files in os.walk('.'):
        # Skip node_modules and .git
        dirs[:] = [d for d in dirs if d not in ['node_modules', '.git', '__pycache__']]

        for file in files:
            if file == 'Dockerfile':
                dockerfiles.append(os.path.join(root, file))
    return sorted(dockerfiles)


def extract_port(content):
    """Extract EXPOSE port from Dockerfile content."""
    match = re.search(r'^EXPOSE\s+(\d+)', content, re.MULTILINE)
    return match.group(1) if match else None


def has_healthcheck_script(content):
    """Check if Dockerfile already has healthcheck.py."""
    return 'COPY scripts/healthcheck.py' in content


def add_healthcheck(dockerfile_path):
    """Add standardized healthcheck to a Dockerfile."""
    with open(dockerfile_path, 'r') as f:
        content = f.read()

    # Skip if already has healthcheck script
    if has_healthcheck_script(content):
        return 'already_has'

    # Extract port
    port = extract_port(content)
    if not port:
        return 'no_port'

    # Healthcheck script lines
    healthcheck_copy = """# Copy standard healthcheck script
COPY scripts/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 755 /usr/local/bin/healthcheck.py

"""

    # HEALTHCHECK command
    healthcheck_cmd = f"""# Health check (using /healthz for Kubernetes compatibility)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \\
    CMD python3 /usr/local/bin/healthcheck.py http://localhost:{port}/healthz

"""

    # Try to find insertion point for healthcheck.py copy
    # Priority: 1) Before "# Create non-root user", 2) Before "RUN useradd", 3) Before "USER", 4) Before "EXPOSE"
    insert_point = None

    if '# Create non-root user' in content:
        insert_point = content.find('# Create non-root user')
    elif re.search(r'^RUN useradd', content, re.MULTILINE):
        match = re.search(r'^RUN useradd', content, re.MULTILINE)
        insert_point = match.start()
    elif re.search(r'^USER\s+', content, re.MULTILINE):
        match = re.search(r'^USER\s+', content, re.MULTILINE)
        insert_point = match.start()
    elif re.search(r'^EXPOSE\s+', content, re.MULTILINE):
        match = re.search(r'^EXPOSE\s+', content, re.MULTILINE)
        # Insert after EXPOSE
        insert_point = content.find('\n', match.end()) + 1

    if insert_point is None:
        return 'no_insert_point'

    # Insert healthcheck script copy
    content = content[:insert_point] + healthcheck_copy + content[insert_point:]

    # Now add HEALTHCHECK command
    # Try to find where to add it: before CMD or ENTRYPOINT
    healthcheck_insert = None

    if re.search(r'^CMD\s+', content, re.MULTILINE):
        match = re.search(r'^CMD\s+', content, re.MULTILINE)
        healthcheck_insert = match.start()
    elif re.search(r'^ENTRYPOINT\s+', content, re.MULTILINE):
        match = re.search(r'^ENTRYPOINT\s+', content, re.MULTILINE)
        healthcheck_insert = match.start()

    if healthcheck_insert is not None:
        # Check if there's already a HEALTHCHECK
        if re.search(r'^HEALTHCHECK\s+', content, re.MULTILINE):
            # Replace existing HEALTHCHECK
            content = re.sub(
                r'HEALTHCHECK\s+.*?CMD[^\n]+',
                f'HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \\\n    CMD python3 /usr/local/bin/healthcheck.py http://localhost:{port}/healthz',
                content,
                flags=re.DOTALL
            )
        else:
            # Add new HEALTHCHECK
            content = content[:healthcheck_insert] + healthcheck_cmd + content[healthcheck_insert:]
    else:
        # No CMD/ENTRYPOINT found, add at the end
        if not re.search(r'^HEALTHCHECK\s+', content, re.MULTILINE):
            content = content.rstrip() + '\n\n' + healthcheck_cmd

    # Write back
    with open(dockerfile_path, 'w') as f:
        f.write(content)

    return 'added'


def main():
    print("Adding standardized healthcheck to all Dockerfiles...\n")

    dockerfiles = find_dockerfiles()
    print(f"Found {len(dockerfiles)} Dockerfiles\n")

    stats = {'added': 0, 'already_has': 0, 'no_port': 0, 'no_insert_point': 0}

    for dockerfile in dockerfiles:
        result = add_healthcheck(dockerfile)
        stats[result] += 1

        status_msg = {
            'added': '✓ Added healthcheck',
            'already_has': '✓ Already has healthcheck',
            'no_port': '⚠️  No EXPOSE port found',
            'no_insert_point': '⚠️  Could not find insertion point'
        }

        print(f"{dockerfile:70s} {status_msg[result]}")

    print("\n" + "=" * 80)
    print("Summary:")
    print(f"  Added: {stats['added']}")
    print(f"  Already had: {stats['already_has']}")
    print(f"  No port found: {stats['no_port']}")
    print(f"  No insertion point: {stats['no_insert_point']}")
    print("=" * 80)


if __name__ == '__main__':
    main()
