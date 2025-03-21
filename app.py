from flask import Flask, render_template, request, jsonify
import paramiko
import os
from werkzeug.utils import secure_filename
import threading
import uuid
import time

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Ensure uploads directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Global dict to store installation status
installation_status = {}

def get_base_installation_commands(os_type, mongo_version):
    """Generate OS-specific MongoDB installation commands"""
    if "Ubuntu" in os_type:
        mongo_specific_version = f"{mongo_version}.4"
        return [
            f"sudo rm -f /usr/share/keyrings/mongodb-server-{mongo_version}.gpg"
            "sudo apt-get update",
            "sudo apt-get install -y gnupg curl",
            f"curl -fsSL https://www.mongodb.org/static/pgp/server-{mongo_version}.asc | \
   sudo gpg -o /usr/share/keyrings/mongodb-server-{mongo_version}.gpg \
   --dearmor",
            f'echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-{mongo_version}.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/{mongo_version} multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-{mongo_version}.list',
            "sudo apt-get update",
            f"sudo apt-get install -y mongodb-org={mongo_specific_version} mongodb-org-database={mongo_specific_version} mongodb-org-server={mongo_specific_version} mongodb-mongosh mongodb-org-mongos={mongo_specific_version} mongodb-org-tools={mongo_specific_version}"
        ]
    elif "Amazon Linux" in os_type or "RHEL" in os_type:
        mongo_specific_version = f"{mongo_version}.4"
        return [
            f"sudo rm -f /etc/yum.repos.d/mongodb-org-{mongo_version}.repo"
            "sudo yum clean all",
            "sudo yum update -y",
            f'echo "[mongodb-org-{mongo_version}]\nname=MongoDB Repository\nbaseurl=https://repo.mongodb.org/yum/amazon/2023/mongodb-org/{mongo_version}/x86_64/\ngpgcheck=1\nenabled=1\ngpgkey=https://www.mongodb.org/static/pgp/server-{mongo_version}.asc" | sudo tee /etc/yum.repos.d/mongodb-org-{mongo_version}.repo',
            "sudo yum clean all",
            "sudo yum update -y",
            f"sudo yum install -y mongodb-org-{mongo_specific_version} mongodb-org-database-{mongo_specific_version} mongodb-org-server-{mongo_specific_version} mongodb-mongosh mongodb-org-mongos-{mongo_specific_version} mongodb-org-tools-{mongo_specific_version}"
        ]
    else:
        raise Exception(f"Unsupported OS type: {os_type}")

def configure_replica_set(ssh, node_type, primary_ip, secondary_ip=None):
    # Create MongoDB configuration for replica set
    config_commands = [
        # Stop MongoDB if running
        "sudo systemctl stop mongod",
        
        # Backup original config
        "sudo cp /etc/mongod.conf /etc/mongod.conf.backup",
        
        # Update MongoDB configuration
        """sudo bash -c 'cat > /etc/mongod.conf << EOL

# Where and how to store data.
storage:
  dbPath: /var/lib/mongodb

# where to write logging data.
systemLog:
  destination: file
  logAppend: true
  path: /var/log/mongodb/mongod.log

# how the process runs
processManagement:
  timeZoneInfo: /usr/share/zoneinfo

# network interfaces
net:
  port: 27017
  bindIp: 0.0.0.0  # Enter 0.0.0.0,:: to bind to all IPv4 and IPv6 addresses or, alternatively, use the net.bindIpAll setting.


#security:

#operationProfiling:

replication:
  replSetName: "rs0"
#sharding:

## Enterprise-Only Options

#auditLog:
#EOL'""",

        # Start MongoDB
        "sudo systemctl start mongod",
        "sudo systemctl enable mongod"
    ]
    
    for cmd in config_commands:
        stdin, stdout, stderr = ssh.exec_command(cmd)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            error_output = stderr.read().decode('utf-8')
            raise Exception(f"Failed to execute: {cmd}. Error: {error_output}")
    
    # Wait for MongoDB to start
    ssh.exec_command("sleep 10")
    
    # Use mongosh for newer MongoDB versions, fallback to mongo for older versions
    shell_cmd = "mongosh"
    
    # Test if mongosh is available
    stdin, stdout, stderr = ssh.exec_command(f"which {shell_cmd}")
    if stdout.channel.recv_exit_status() != 0:
        shell_cmd = "mongo"  # Fallback to older mongo shell
    
    # Initialize replica set on primary node
    if node_type == "primary":
        # Use the actual IP addresses for replica set configuration
        init_command = f"""{shell_cmd} --eval 'rs.initiate({{
            _id: "rs0",
            members: [
                {{ _id: 0, host: "{primary_ip}:27017", priority: 2 }}
            ]
        }})'"""

        stdin, stdout, stderr = ssh.exec_command(init_command)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            error_output = stderr.read().decode('utf-8')
            raise Exception(f"Failed to initialize replica set. Error: {error_output}")
        
        # Get the initialization output to verify
        init_output = stdout.read().decode('utf-8')
        if "ok" not in init_output.lower():
            raise Exception(f"Replica set initialization didn't return ok: {init_output}")
            
    # Add secondary node to replica set
    elif node_type == "secondary" and primary_ip:
        # Wait a bit longer to ensure primary is ready
        ssh.exec_command("sleep 15")
        
        # Add secondary node to the replica set
        add_secondary_command = f"""{shell_cmd} --host {primary_ip} --eval 'rs.add("{secondary_ip}:27017")'"""
        
        stdin, stdout, stderr = ssh.exec_command(add_secondary_command)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            error_output = stderr.read().decode('utf-8')
            raise Exception(f"Failed to add secondary node to replica set. Error: {error_output}")
        
        # Verify secondary was added
        secondary_output = stdout.read().decode('utf-8')
        if "ok" not in secondary_output.lower():
            raise Exception(f"Secondary node addition didn't return ok: {secondary_output}")

def install_mongodb(task_id, primary_ip, secondary_ip, key_path, os_type, mongo_version):
    """Install MongoDB on primary and secondary nodes"""
    try:
        installation_status[task_id] = {"status": "installing", "message": "Starting installation..."}
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Determine username based on OS type
        username = "ec2-user" if "Amazon" in os_type or "RHEL" in os_type else "ubuntu"
        
        # Install on Primary Node
        installation_status[task_id] = {"status": "installing", "message": "Installing MongoDB on primary node..."}
        ssh.connect(hostname=primary_ip, username=username, key_filename=key_path)
        
        # Install MongoDB on primary
        commands = get_base_installation_commands(os_type, mongo_version)
        for cmd in commands:
            stdin, stdout, stderr = ssh.exec_command(cmd)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error_output = stderr.read().decode('utf-8')
                raise Exception(f"Failed to execute on primary: {cmd}. Error: {error_output}")
        
        # Configure primary node
        installation_status[task_id] = {"status": "installing", "message": "Configuring primary node replica set..."}
        configure_replica_set(ssh, "primary", primary_ip, secondary_ip)
        ssh.close()
        
        # Install on Secondary Node
        installation_status[task_id] = {"status": "installing", "message": "Installing MongoDB on secondary node..."}
        ssh.connect(hostname=secondary_ip, username=username, key_filename=key_path)
        
        # Install MongoDB on secondary
        for cmd in commands:
            stdin, stdout, stderr = ssh.exec_command(cmd)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error_output = stderr.read().decode('utf-8')
                raise Exception(f"Failed to execute on secondary: {cmd}. Error: {error_output}")
        
        # Configure secondary node
        installation_status[task_id] = {"status": "installing", "message": "Adding secondary node to replica set..."}
        configure_replica_set(ssh, "secondary", primary_ip, secondary_ip)
        
        # Verify replica set status
        installation_status[task_id] = {"status": "installing", "message": "Verifying replica set configuration..."}
        stdin, stdout, stderr = ssh.exec_command("mongosh --eval 'rs.status()'")
        
        installation_status[task_id] = {
            "status": "completed",
            "message": "MongoDB replica set installation completed successfully!"
        }
        
    except Exception as e:
        installation_status[task_id] = {
            "status": "error",
            "message": f"Installation failed: {str(e)}"
        }
    finally:
        if 'ssh' in locals() and ssh.get_transport() and ssh.get_transport().is_active():
            ssh.close()
        try:
            os.remove(key_path)
        except:
            pass

@app.route('/')
def index():
    """Render the main page with form"""
    mongo_versions = ["8.0", "6.0", "5.0", "4.4", "4.2"]
    os_types = [
        "Ubuntu 22.04",
        "Ubuntu 20.04",
        "Amazon Linux 2",
        "RHEL 8",
        "RHEL 7"
    ]
    return render_template('index.html', mongo_versions=mongo_versions, os_types=os_types)

@app.route('/install', methods=['POST'])
def start_installation():
    """Handle form submission and start installation"""
    if 'key_file' not in request.files:
        return jsonify({"error": "No key file provided"}), 400
    
    key_file = request.files['key_file']
    if key_file.filename == '':
        return jsonify({"error": "No key file selected"}), 400
    
    if not key_file.filename.endswith('.pem'):
        return jsonify({"error": "Invalid key file format. Must be .pem"}), 400
    
    # Save the key file
    filename = secure_filename(f"{uuid.uuid4().hex}_{key_file.filename}")
    key_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    key_file.save(key_path)
    os.chmod(key_path, 0o600)  # Set proper permissions for SSH key
    
    # Get form data
    primary_ip = request.form.get('primary_ip')
    secondary_ip = request.form.get('secondary_ip')
    os_type = request.form.get('os_type')
    mongo_version = request.form.get('mongo_version')
    
    if not primary_ip or not secondary_ip:
        return jsonify({"error": "Both primary and secondary IP addresses are required"}), 400
    
    if not os_type or not mongo_version:
        return jsonify({"error": "OS type and MongoDB version must be specified"}), 400
    
    # Generate unique task ID
    task_id = f"task_{uuid.uuid4().hex}"
    
    # Start installation in a separate thread
    thread = threading.Thread(
        target=install_mongodb,
        args=(task_id, primary_ip, secondary_ip, key_path, os_type, mongo_version)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({"task_id": task_id, "message": "Installation started"})

@app.route('/status/<task_id>')
def get_status(task_id):
    """Get the status of an installation task"""
    return jsonify(installation_status.get(task_id, {
        "status": "not_found",
        "message": "Installation task not found"
    }))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)