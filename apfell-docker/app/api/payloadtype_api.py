from app import apfell, db_objects
from sanic.response import json
from app.database_models.model import PayloadType, Command, CommandParameters, CommandTransform, ATTACKCommand, \
    PayloadTypeC2Profile, Transform, ArtifactTemplate
from sanic_jwt.decorators import scoped, inject_user
from urllib.parse import unquote_plus
import os
from shutil import rmtree
import pathlib
import json as js
import glob
import base64, datetime
import app.database_models.model as db_model
from app.api.rabbitmq_api import send_pt_rabbitmq_message
from sanic.exceptions import abort
from sanic.log import logger
from peewee import fn
import uuid
from app.api.transform_api import write_transforms_to_file, update_all_pt_transform_code


# payloadtypes aren't inherent to an operation
@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/", methods=['GET'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def get_all_payloadtypes(request, user):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    query = await db_model.payloadtype_query()
    payloads = await db_objects.execute(query.where(db_model.PayloadType.deleted == False))
    return json([p.to_json() for p in payloads])


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>", methods=['GET'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def get_one_payloadtype(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        return json({'status': 'error', 'error': 'failed to find payload type'})
    return json({'status': 'success', **payloadtype.to_json()})


# anybody can create a payload type for now, maybe just admins in the future?
@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/", methods=['POST'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def create_payloadtype(request, user):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    # this needs to know the name of the type, everything else is done for you
    if request.form:
        data = js.loads(request.form.get('json'))
    else:
        data = request.json
    try:
        if "ptype" not in data:
            return json({'status': 'error', 'error': '"ptype" is a required field and must be unique'})
        if "file_extension" not in data:
            data["file_extension"] = ""
        elif "." in data['file_extension'] and data['file_extension'][0] == ".":
            data['file_extension'] = data['file_extension'][1:]
        if 'wrapper' not in data:
            data['wrapper'] = False
        if "command_template" not in data or data['command_template'] == "":
            data['command_template'] = "\n"
        if 'supported_os' not in data:
            return json({'status': 'error', 'error': 'must specify "supported_os" list'})
        if 'execute_help' not in data:
            data['execute_help'] = ""
        if 'external' not in data:
            data['external'] = False
        if 'note' not in data:
            data['note'] = ""
        if 'author' not in data:
            data['author'] = user['username']
        if 'supports_dynamic_loading' not in data:
            data['supports_dynamic_loading'] = False
        query = await db_model.operator_query()
        operator = await db_objects.get(query, username=user['username'])
        data['ptype'] = data['ptype'].replace(" ", "_")
        if data['wrapper']:
            if "wrapped_payload_type" not in data:
                return json(
                    {'status': 'error', 'error': '"wrapped_payload_type" is required for a wrapper type payload'})
            try:
                query = await db_model.payloadtype_query()
                wrapped_payload_type = await db_objects.get(query, ptype=data['wrapped_payload_type'])
            except Exception as e:
                logger.exception("exception in create_payloadtype when creating a wrapper")
                return json({'status': 'error', 'error': "failed to find that wrapped payload type"})
            payloadtype = await db_objects.create(PayloadType, ptype=data['ptype'], operator=operator,
                                                  file_extension=data['file_extension'],
                                                  wrapper=data['wrapper'],
                                                  wrapped_payload_type=wrapped_payload_type,
                                                  supported_os=",".join(data['supported_os']),
                                                  execute_help=data['execute_help'],
                                                  external=data['external'], container_running=False,
                                                  author=data['author'], supports_dynamic_loading=data['supports_dynamic_loading'],
                                                  note=data['note'])
        else:
            payloadtype = await db_objects.create(PayloadType, ptype=data['ptype'], operator=operator,
                                                  file_extension=data['file_extension'],
                                                  wrapper=data['wrapper'], command_template=data['command_template'],
                                                  supported_os=",".join(data['supported_os']),
                                                  execute_help=data['execute_help'],
                                                  external=data['external'], container_running=False,
                                                  author=data['author'], supports_dynamic_loading=data['supports_dynamic_loading'],
                                                  note=data['note'])
        pathlib.Path("./app/payloads/{}".format(payloadtype.ptype)).mkdir(parents=True, exist_ok=True)
        #os.mkdir("./app/payloads/{}".format(payloadtype.ptype))  # make the directory structure
        pathlib.Path("./app/payloads/{}/payload".format(payloadtype.ptype)).mkdir(parents=True, exist_ok=True)
        #os.mkdir("./app/payloads/{}/payload".format(payloadtype.ptype))  # make the directory structure
        pathlib.Path("./app/payloads/{}/commands".format(payloadtype.ptype)).mkdir(parents=True, exist_ok=True)
        #os.mkdir("./app/payloads/{}/commands".format(payloadtype.ptype))  # make the directory structure
        if request.files:
            code = request.files['upload_file'][0].body
            code_file = open(
                "./app/payloads/{}/payload/{}".format(payloadtype.ptype, request.files['upload_file'][0].name), "wb")
            code_file.write(code)
            code_file.close()
            for i in range(1, int(request.form.get('file_length'))):
                code = request.files['upload_file_' + str(i)][0].body
                code_file = open("./app/payloads/{}/payload/{}".format(payloadtype.ptype,
                                                                       request.files['upload_file_' + str(i)][0].name),
                                 "wb")
                code_file.write(code)
                code_file.close()
    except Exception as e:
        logger.exception("exception in create_payloadtype")
        return json({'status': 'error', 'error': 'failed to create new payload type: ' + str(e)})
    status = {'status': 'success'}
    ptype_json = payloadtype.to_json()
    return json({**status, **ptype_json})


# anybody can create a payload type for now, maybe just admins in the future?
@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>", methods=['PUT'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def update_payloadtype(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    if request.form:
        data = js.loads(request.form.get('json'))
    else:
        data = request.json
    try:
        payload_type = unquote_plus(ptype)
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        logger.exception("exception in update_payloadtype")
        return json({'status': 'error', 'error': "failed to find that payload type"})
    query = await db_model.operator_query()
    operator = await db_objects.get(query, username=user['username'])
    if user['admin'] or payloadtype.operator == operator:
        if 'file_extension' in data:
            if data['file_extension'] == "":
                payloadtype.file_extension = ""
            elif "." in data['file_extension'][0]:
                payloadtype.file_extension = data['file_extension'][1:]
            else:
                payloadtype.file_extension = data['file_extension']
        if 'wrapper' in data:
            payloadtype.wrapper = data['wrapper']
        if 'wrapped_payload_type' in data:
            try:
                query = await db_model.payloadtype_query()
                wrapped_payload_type = await db_objects.get(query, ptype=data['wrapped_payload_type'])
            except Exception as e:
                logger.exception("exception in update_payloadtype")
                return json({'status': 'error', 'error': "failed to find that wrapped payload type"})
            payloadtype.wrapped_payload_type = wrapped_payload_type
        if 'command_template' in data:
            payloadtype.command_template = data['command_template']
        if 'supported_os' in data:
            payloadtype.supported_os = ",".join(data['supported_os'])
        if 'execute_help' in data:
            payloadtype.execute_help = data['execute_help']
        if 'external' in data:
            payloadtype.external = data['external']
        if 'container_running' in data:
            payloadtype.container_running = data['container_running']
        if 'author' in data:
            payloadtype.author = data['author']
        if 'supports_dynamic_loading' in data:
            payloadtype.supports_dynamic_loading = data['supports_dynamic_loading']
        if 'note' in data:
            payloadtype.note = data['note']
        await db_objects.update(payloadtype)
        return json({'status': 'success', **payloadtype.to_json()})
    else:
        return json({'status': 'error', 'error': "must be an admin or the creator of the type to edit it"})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/upload", methods=['POST'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def upload_payload_code(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        logger.exception("exception in update_payload_code")
        return json({'status': 'error', 'error': 'failed to find payload'})
    base_path = "./app/payloads/{}/payload/".format(payloadtype.ptype)
    try:
        data = js.loads(request.form.get('json'))
        path = base_path + "/" + data['folder'] if 'folder' in data else base_path
        if base_path in path:
            base_path = path
    except Exception as e:
        pass
    uploaded_files = []
    try:
        if request.files:
            code = request.files['upload_file'][0].body
            code_file = open(base_path + "/{}".format(request.files['upload_file'][0].name), "wb")
            code_file.write(code)
            code_file.close()
            uploaded_files.append(request.files['upload_file'][0].name)
            for i in range(1, int(request.form.get('file_length'))):
                code = request.files['upload_file_' + str(i)][0].body
                code_file = open(
                    base_path + "/{}".format(request.files['upload_file_' + str(i)][0].name), "wb")
                code_file.write(code)
                code_file.close()
                uploaded_files.append(request.files['upload_file_' + str(i)][0].name)
            return json({'status': 'success', 'files': uploaded_files})
        else:
            return json({'status': 'error', 'error': 'nothing to upload...'})
    except Exception as e:
        logger.exception("exception in upload_payload_code")
        return json({'status': 'error', 'error': str(e)})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/container_upload", methods=['POST'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def upload_payload_container_code(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        logger.exception("exception in upload_payload_container_code")
        return json({'status': 'error', 'error': 'failed to find payload'})
    if request.files:
        code = request.files['upload_file'][0].body
        status = await send_pt_rabbitmq_message(payload_type, "writefile",
                                                base64.b64encode(
                                                    js.dumps(
                                                        {"file_path": request.files['upload_file'][0].name,
                                                         "data": base64.b64encode(code).decode('utf-8')}).encode()
                                                ).decode('utf-8'), user['username'])
        for i in range(1, int(request.form.get('file_length'))):
            code = request.files['upload_file_' + str(i)][0].body
            status = await send_pt_rabbitmq_message(payload_type, "writefile",
                                                    base64.b64encode(
                                                        js.dumps(
                                                            {"file_path": request.files['upload_file_' + str(i)][
                                                                0].name,
                                                             "data": base64.b64encode(code).decode('utf-8')}).encode()
                                                    ).decode('utf-8'), user['username'])
        return json(status)
    else:
        return json({'status': 'error', 'error': 'nothing to upload...'})


# payloadtypes aren't inherent to an operation
@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/<fromDisk:int>", methods=['DELETE'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def delete_one_payloadtype(request, user, ptype, fromDisk):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        return json({'status': 'error', 'error': 'failed to find payload type'})
    query = await db_model.operator_query()
    operator = await db_objects.get(query, username=user['username'])
    if payloadtype.operator == operator or user['admin']:
        # only delete a payload type if you created it or if you're an admin
        try:
            payloadtype_json = payloadtype.to_json()
            payloadtype.deleted = True
            payloadtype.ptype = str(uuid.uuid4()) + " ( " + payloadtype.ptype + " )"
            await db_objects.update(payloadtype)
            # await db_objects.delete(payloadtype, recursive=True)
            if fromDisk == 1:
                # this means we should delete the corresponding folder from disk as well
                try:
                    rmtree("./app/payloads/{}".format(payloadtype_json['ptype']))
                except Exception as e:
                    print("Directory didn't exist")
            query = await db_model.payloadtypec2profile_query()
            mapping = await db_objects.execute(query.where(db_model.PayloadTypeC2Profile.payload_type == payloadtype))
            for m in mapping:
                if fromDisk == 1:
                    try:
                        # remove the payload from all associated c2 profile mappings
                        for opname in glob.iglob("./app/c2_profiles/"):
                            rmtree(opname + "/{}".format(payloadtype_json['ptype']))
                    except Exception as e:
                        print("Failed to remove directory")
                await db_objects.delete(m)
            return json({'status': 'success', **payloadtype_json})
        except Exception as e:
            logger.exception("exception in delete_one_payloadtype")
            return json({'status': 'error', 'error': 'failed to delete payloadtype. ' + str(e)})
    else:
        return json({'status': 'error', 'error': 'you must be admin or the creator of the payload type to delete it'})


# get all the commands associated with a specitic payload_type
@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/commands", methods=['GET'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def get_commands_for_payloadtype(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'failed to get payload type'})
    query = await db_model.command_query()
    commands = await db_objects.execute(query.where(
        (Command.payload_type == payloadtype) &
        (Command.deleted == False)
    ).order_by(Command.cmd))
    all_commands = []
    for cmd in commands:
        query = await db_model.commandparameters_query()
        params = await db_objects.execute(query.where(CommandParameters.command == cmd))
        query = await db_model.commandtransform_query()
        transforms = await db_objects.execute(query.where(CommandTransform.command == cmd))
        all_commands.append(
            {**cmd.to_json(), "params": [p.to_json() for p in params], "transforms": [t.to_json() for t in transforms]})
    status = {'status': 'success'}
    return json({**status, 'commands': all_commands})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/files", methods=['GET'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def list_uploaded_files_for_payloadtype(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'failed to get payload type'})
    try:
        path = "./app/payloads/{}/payload/".format(payload_type)
        files = []
        for (dirpath, dirnames, filenames) in os.walk(path):
            files.append({"folder": dirpath.replace(path, ""), "dirnames": dirnames, "filenames": filenames})
        return json({'status': 'success', 'files': files})
    except Exception as e:
        logger.exception("exception in list_uploaded_files_for_payloadtype")
        return json({'status': 'error', 'error': 'failed getting files: ' + str(e)})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/files/add_folder", methods=['POST'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def add_folder_for_payloadtype(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
        data = request.json
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'failed to get payload type'})
    try:
        payload_type_path = "./app/payloads/{}/payload/".format(payload_type)
        payload_type_path = os.path.abspath(payload_type_path)
        if data['folder'] == "" or data['folder'] is None:
            data['folder'] = "."
        path = payload_type_path + "/" + data['folder'] + "/" + data['sub_folder']
        path_abs = os.path.abspath(path)
        if payload_type_path in path_abs:
            os.mkdir(path_abs)
            added_path = path_abs[len(payload_type_path):]
            return json({'status': 'success', 'folder': added_path})
        else:
            return json({'status': 'error', 'error': 'trying to create a folder outside your payload type'})
    except Exception as e:
        logger.exception("exception in add_folder_for_payloadtype")
        return json({'status': 'error', 'error': 'failed creating folder: ' + str(e)})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/files/remove_folder", methods=['POST'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def remove_folder_for_payloadtype(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
        data = request.json
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'failed to get payload type'})
    try:
        payload_type_path = "./app/payloads/{}/payload/".format(payload_type)
        payload_type_path = os.path.abspath(payload_type_path)
        path = payload_type_path + "/" + data['folder']
        path = os.path.abspath(path)
        if payload_type_path in path and payload_type_path != path:
            os.rmdir(path)
            return json({'status': 'success', 'folder': path})
        else:
            return json({'status': 'error',
                         'error': 'trying to remove a folder outside your payload type or one that isn\'t empty'})
    except Exception as e:
        logger.exception("exception in remove_folder_for_payloadtype")
        return json({'status': 'error', 'error': 'failed to remove folder: ' + str(e)})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/container_files", methods=['GET'])
@inject_user()
@scoped('auth:user')
async def list_uploaded_container_files_for_payloadtype(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    # apitoken for this won't help much since it's rabbitmq based
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'failed to get payload type'})
    try:
        status = await send_pt_rabbitmq_message(payload_type, "listfiles", "", user['username'])
        return json(status)
    except Exception as e:
        logger.exception("exception in list_uploaded_contianer_files_for_payloadtype")
        return json({'status': 'error', 'error': 'failed getting files: ' + str(e)})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/files/delete", methods=['POST'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def remove_uploaded_files_for_payloadtype(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'failed to get payload type'})
    try:
        data = request.json
        path = os.path.abspath("./app/payloads/{}/payload/".format(payload_type))
        if data['folder'] == "" or data['folder'] is None:
            data['folder'] = "."
        attempted_path = os.path.abspath(path + "/" + data['folder'] + "/" + data['file'])
        if path in attempted_path:
            os.remove(attempted_path)
            if data['folder'] == ".":
                data['folder'] = ""
            return json({'status': 'success', 'folder': data['folder'], 'file': data['file']})
        return json({'status': 'error', 'error': 'failed to find file'})
    except Exception as e:
        logger.exception("exception in remove_uploaded_files_for_payloadtype")
        return json({'status': 'error', 'error': 'failed getting files: ' + str(e)})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/files/container_delete", methods=['POST'])
@inject_user()
@scoped('auth:user')
async def remove_uploaded_container_files_for_payloadtype(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    # apitoken access for this won't help since it's rabbitmq based
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'failed to get payload type'})
    try:
        data = request.json
        status = await send_pt_rabbitmq_message(payload_type, "removefile",
                                                base64.b64encode(js.dumps({
                                                    "folder": data['folder'],
                                                    "file": data['file']
                                                }).encode()).decode('utf-8'), user['username'])
        return json(status)
    except Exception as e:
        logger.exception("exception in remove_uploaded_container_files_for_payloadtype")
        return json({'status': 'error', 'error': 'failed sending message: ' + str(e)})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/files/download", methods=['POST'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def download_file_for_payloadtype(request, ptype, user):
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'failed to find payload type'})
    try:
        data = request.json
        if 'file' not in data:
            return json({'status': 'error', 'error': 'failed to get file parameter'})
        if 'folder' not in data:
            data['folder'] = "."
        path = os.path.abspath("./app/payloads/{}/payload/".format(payload_type))
        if data['folder'] == "":
            data['folder'] = "."
        attempted_path = os.path.abspath(path + "/" + data['folder'] + "/" + data['file'])
        if path in attempted_path:
            code = open(attempted_path, 'rb')
            encoded = base64.b64encode(code.read()).decode("UTF-8")
            return json({"status": "success", "file": encoded})
        return json({'status': 'error', 'error': 'failed to read file'})
    except Exception as e:
        logger.exception("exception in download_file_for_payloadtype")
        return json({'status': 'error', 'error': 'failed finding the file: ' + str(e)})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/files/container_download", methods=['POST'])
@inject_user()
@scoped('auth:user')
async def download_container_file_for_payloadtype(request, ptype, user):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    # apitoken access for this own't help since it's rabbitmq based
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payloadtype = await db_objects.get(query, ptype=payload_type)
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'failed to find payload type'})
    try:
        data = request.json
        status = await send_pt_rabbitmq_message(payload_type, "getfile",
                                                base64.b64encode(js.dumps(data).encode()).decode('utf-8'), user['username'])
        return json(status)
    except Exception as e:
        logger.exception("exception in download_container_file_for_payloadtype")
        return json({'status': 'error', 'error': 'failed sending the message: ' + str(e)})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/export", methods=['GET'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def export_command_list(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payload_ptype = await db_objects.get(query, ptype=payload_type)
        query = await db_model.operator_query()
        operator = await db_objects.get(query, username=user['username'])
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'unable to find that payload type'})
    cmdlist = []
    try:
        payloadtype_json = payload_ptype.to_json()
        del payloadtype_json['id']
        del payloadtype_json['operator']
        del payloadtype_json['creation_time']
        payloadtype_json['files'] = []
        if not payload_ptype.external:
            for file in glob.iglob("./app/payloads/{}/payload/**".format(payload_type), recursive=True):
                if os.path.isdir(file):
                    continue
                payload_file = open(file, 'rb')
                pathname = file.replace("./app/payloads/{}/payload/".format(payload_type), "")
                file_dict = {pathname: base64.b64encode(payload_file.read()).decode('utf-8')}
                payloadtype_json['files'].append(file_dict)
        transforms = {}
        browser_scripts = []
        browserscriptquery = await db_model.browserscript_query()
        bscripts = await db_objects.execute(browserscriptquery.where( (db_model.BrowserScript.command == None) & (db_model.BrowserScript.operator == operator )))
        for script in bscripts:
            browser_scripts.append({"name": script.name, "script": script.script})
        payloadtype_json['support_scripts'] = browser_scripts
        query = await db_model.command_query()
        commands = await db_objects.execute(query.where(
            (Command.payload_type == payload_ptype) & (Command.deleted == False)))
        for c in commands:
            cmd_json = c.to_json()
            del cmd_json['id']
            del cmd_json['creation_time']
            del cmd_json['operator']
            del cmd_json['payload_type']
            query = await db_model.commandparameters_query()
            params = await db_objects.execute(query.where(CommandParameters.command == c))
            params_list = []
            for p in params:
                p_json = p.to_json()
                del p_json['id']
                del p_json['command']
                del p_json['cmd']
                del p_json['operator']
                del p_json['payload_type']
                params_list.append(p_json)
            cmd_json['parameters'] = params_list
            query = await db_model.attackcommand_query()
            attacks = await db_objects.execute(query.where(ATTACKCommand.command == c))
            attack_list = []
            for a in attacks:
                a_json = a.to_json()
                del a_json['command']
                del a_json['command_id']
                del a_json['id']
                attack_list.append(a_json)
            cmd_json['attack'] = attack_list
            query = await db_model.artifacttemplate_query()
            artifacts = await db_objects.execute(
                query.where((ArtifactTemplate.command == c) & (ArtifactTemplate.deleted == False)))
            artifact_list = []
            for a in artifacts:
                a_json = {"command_parameter": a.command_parameter.name if a.command_parameter else "null",
                          "artifact": bytes(a.artifact.name).decode(),
                          "artifact_string": a.artifact_string, "replace_string": a.replace_string}
                artifact_list.append(a_json)
            cmd_json['artifacts'] = artifact_list
            cmd_json['files'] = []
            try:
                for cmd_file in glob.iglob("./app/payloads/{}/commands/{}/**".format(payload_type, c.cmd),
                                           recursive=True):
                    if os.path.isdir(cmd_file):
                        continue
                    cmd_name = cmd_file.replace("./app/payloads/{}/commands/{}/".format(payload_type, c.cmd), "")
                    file_data = open(cmd_file, 'rb')
                    file_dict = {cmd_name: base64.b64encode(file_data.read()).decode('utf-8')}
                    cmd_json['files'].append(file_dict)
            except Exception as e:
                logger.exception("exception in exporting a payload type's command's")
                pass
            query = await db_model.commandtransform_query()
            command_transforms = await db_objects.execute(query.where(db_model.CommandTransform.command == c))
            cmd_transforms = []
            for ct in command_transforms:
                ct_json = ct.to_json()
                del ct_json['id']
                del ct_json['operator']
                transforms[ct.transform.name] = ct.transform.to_json()
                cmd_transforms.append(ct_json)
            cmd_json['transforms'] = cmd_transforms
            try:
                bscript = await db_objects.get(browserscriptquery, command=c)
                cmd_json['browser_script'] = bscript.script
            except Exception as e:
                pass
            cmdlist.append(cmd_json)
        # get all the c2 profiles we can that match up with this payload type for the current operation
        query = await db_model.payloadtypec2profile_query()
        profiles = await db_objects.execute(query.where(PayloadTypeC2Profile.payload_type == payload_ptype))
        profiles_dict = {}
        for p in profiles:
            files = []
            if not payload_ptype.external:
                for profile_file in glob.iglob("./app/c2_profiles/{}/{}/*".format(p.c2_profile.name, payload_type)):
                    file_contents = open(profile_file, 'rb')
                    file_dict = {profile_file.split("/")[-1]: base64.b64encode(file_contents.read()).decode('utf-8')}
                    files.append(file_dict)
            profiles_dict[p.c2_profile.name] = files
        payloadtype_json['c2_profiles'] = profiles_dict
        # get all of the module load transformations
        query = await db_model.transform_query()
        load_transforms = await db_objects.execute(query.where(
            (Transform.t_type == "load") & (Transform.payload_type == payload_ptype)))
        load_transforms_list = []
        for lt in load_transforms:
            lt_json = lt.to_json()
            del lt_json['payload_type']
            del lt_json['operator']
            del lt_json['timestamp']
            del lt_json['t_type']
            del lt_json['id']
            transforms[lt.transform.name] = lt.transform.to_json()
            load_transforms_list.append(lt_json)
        payloadtype_json['load_transforms'] = load_transforms_list
        # get all of the payload creation transformations
        query = await db_model.transform_query()
        create_transforms = await db_objects.execute(query.where(
            (Transform.t_type == "create") & (Transform.payload_type == payload_ptype)))
        create_transforms_list = []
        for ct in create_transforms:
            ct_json = ct.to_json()
            del ct_json['payload_type']
            del ct_json['operator']
            del ct_json['timestamp']
            del ct_json['t_type']
            del ct_json['id']
            transforms[ct.transform.name] = ct.transform.to_json()
            create_transforms_list.append(ct_json)
        payloadtype_json['create_transforms'] = create_transforms_list

    except Exception as e:
        logger.exception("exception in exporting a payload type")
        return json({'status': 'error', 'error': 'failed to get information for that payload type: ' + str(e)})
    return json({"payload_types": [{**payloadtype_json, 'transforms': [value for key, value in transforms.items()], "commands": cmdlist}]})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/<ptype:string>/export/commands", methods=['POST'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def export_single_commands(request, user, ptype):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    payload_type = unquote_plus(ptype)
    try:
        query = await db_model.payloadtype_query()
        payload_ptype = await db_objects.get(query, ptype=payload_type)
        query = await db_model.operation_query()
        operation = await db_objects.get(query, name=user['current_operation'])
        cmd_array = request.json['commands']
    except Exception as e:
        logger.exception("failed to find payload type or parse post_data in export_single_commands")
        return json({'status': 'error', 'error': 'unable to find that payload type or parse commands list'})
    try:
        cmd_list = []
        query = await db_model.command_query()
        commands = await db_objects.execute(query.where((
                Command.payload_type == payload_ptype) & (Command.deleted == False)))
        transforms = {}
        for c in commands:
            if c.cmd in cmd_array:
                cmd_json = c.to_json()
                del cmd_json['id']
                del cmd_json['creation_time']
                del cmd_json['operator']
                del cmd_json['payload_type']
                query = await db_model.commandparameters_query()
                params = await db_objects.execute(query.where(CommandParameters.command == c))
                params_list = []
                for p in params:
                    p_json = p.to_json()
                    del p_json['id']
                    del p_json['command']
                    del p_json['cmd']
                    del p_json['operator']
                    del p_json['payload_type']
                    params_list.append(p_json)
                cmd_json['parameters'] = params_list
                query = await db_model.attackcommand_query()
                attacks = await db_objects.execute(query.where(ATTACKCommand.command == c))
                attack_list = []
                for a in attacks:
                    a_json = a.to_json()
                    del a_json['command']
                    del a_json['command_id']
                    del a_json['id']
                    attack_list.append(a_json)
                cmd_json['attack'] = attack_list
                query = await db_model.artifacttemplate_query()
                artifacts = await db_objects.execute(
                    query.where((ArtifactTemplate.command == c) & (ArtifactTemplate.deleted == False)))
                artifact_list = []
                for a in artifacts:
                    a_json = {"command_parameter": a.command_parameter.name if a.command_parameter else "null",
                              "artifact": bytes(a.artifact.name).decode(),
                              "artifact_string": a.artifact_string, "replace_string": a.replace_string}
                    artifact_list.append(a_json)
                cmd_json['artifacts'] = artifact_list
                cmd_json['files'] = []
                try:
                    for cmd_file in glob.iglob("./app/payloads/{}/commands/{}/**".format(payload_type, c.cmd),
                                               recursive=True):
                        if os.path.isdir(cmd_file):
                            continue
                        cmd_name = cmd_file.replace("./app/payloads/{}/commands/{}/".format(payload_type, c.cmd), "")
                        file_data = open(cmd_file, 'rb')
                        file_dict = {cmd_name: base64.b64encode(file_data.read()).decode('utf-8')}
                        cmd_json['files'].append(file_dict)
                except Exception as e:
                    logger.exception("exception in reading command files")
                    pass
                query = await db_model.commandtransform_query()
                command_transforms = await db_objects.execute(query.where(db_model.CommandTransform.command == c))
                cmd_transforms = []
                for ct in command_transforms:
                    ct_json = ct.to_json()
                    del ct_json['id']
                    del ct_json['operator']
                    transforms[ct.transform.name] = ct.transform.to_json()
                    cmd_transforms.append(ct_json)
                cmd_json['transforms'] = cmd_transforms
                cmd_list.append(cmd_json)
        return json({'status': 'success', 'payload_types': [{**payload_ptype.to_json(), 'files': [],
                                                            'load_transforms': [], 'create_transforms': [],
                                                             'c2_profiles':{},
                                                             'transforms': [value for key, value in transforms.items()],
                                                             'commands': cmd_list}]})
    except Exception as e:
        logger.exception("exception in exporting commands")
        return json({'status': 'error', 'error': 'failed to get information for that payload type: ' + str(e)})


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/import", methods=['POST'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def import_payloadtype_and_commands(request, user):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    # The format for this will be the same as the default_commands.json file or what you get from the export function
    # This allows you to import commands across a set of different payload types at once
    if request.files:
        try:
            data = js.loads(request.files['upload_file'][0].body.decode('UTF-8'))
        except Exception as e:
            logger.exception("Failed to parse uploaded json file for importing a payload type")
            return json({'status': 'error', 'error': 'failed to parse file'})
    else:
        try:
            data = request.json
        except Exception as e:
            logger.exception("exception in parsing JSON when importing payload type")
            return json({'status': 'error', 'error': 'failed to parse JSON'})
    try:
        query = await db_model.operator_query()
        operator = await db_objects.get(query, username=user['username'])
        query = await db_model.operation_query()
        operation = await db_objects.get(query, name=user['current_operation'])
    except Exception as e:
        print(e)
        return json({'status': 'error', 'error': 'failed to get operator or current_operation information'})
    if "payload_types" not in data:
        return json({'status': 'error', 'error': 'must start with "payload_types"'})
    # we will need to loop over this twice, once doing non-wrapper payload types, another to do the wrapper types
    # this ensures that wrapped types have a chance to have their corresponding payload type already created
    nonwrapped = [ptype for ptype in data['payload_types'] if not ptype['wrapper']]
    wrapped = [ptype for ptype in data['payload_types'] if ptype['wrapper']]
    for ptype in nonwrapped:
        status = await import_payload_type_func(ptype, operator, operation)
        if status['status'] == 'error':
            return json({'status': 'error', 'error': status['error']})
    for ptype in wrapped:
        status = await import_payload_type_func(ptype, operator, operation)
        if status['status'] == 'error':
            return json({'status': 'error', 'error': status['error']})
    return json({'status': 'success'})


async def import_payload_type_func(ptype, operator, operation):
    try:
        if 'author' not in ptype:
            ptype['author'] = operator.username
        if 'note' not in ptype:
            ptype['note'] = ""
        if ptype['wrapper']:
            try:
                query = await db_model.payloadtype_query()
                wrapped_payloadtype = await db_objects.get(query, ptype=ptype['wrapped_payload_type'])
            except Exception as e:
                return {ptype['ptype']: 'failed to find wrapped payload type'}
            try:
                query = await db_model.payloadtype_query()
                payload_type = await db_objects.get(query, ptype=ptype['ptype'],
                                                    wrapped_payload_type=wrapped_payloadtype)
            except Exception as e:
                # this means we need to create it
                if 'external' not in ptype:
                    ptype['external'] = False
                payload_type = await db_objects.create(PayloadType, ptype=ptype['ptype'],
                                                       wrapped_payload_type=wrapped_payloadtype,
                                                       operator=operator, wrapper=True,
                                                       command_template=ptype['command_template'],
                                                       supported_os=ptype['supported_os'],
                                                       file_extension=ptype['file_extension'],
                                                       execute_help=ptype['execute_help'], external=ptype['external'],
                                                       author=ptype['author'],
                                                       note=ptype['note'],
                                                       supports_dynamic_loading=ptype['supports_dynamic_loading'])

        else:
            try:
                query = await db_model.payloadtype_query()
                payload_type = await db_objects.get(query, ptype=ptype['ptype'])
            except Exception as e:
                if 'external' not in ptype:
                    ptype['external'] = False
                payload_type = await db_objects.create(PayloadType, ptype=ptype['ptype'],
                                                       operator=operator, wrapper=False,
                                                       command_template=ptype['command_template'],
                                                       supported_os=ptype['supported_os'],
                                                       file_extension=ptype['file_extension'],
                                                       execute_help=ptype['execute_help'],
                                                       external=ptype['external'],
                                                       author=ptype['author'],
                                                       note=ptype['note'],
                                                       supports_dynamic_loading=ptype['supports_dynamic_loading'])
        try:
            payload_type.operator = operator
            payload_type.creation_time = datetime.datetime.utcnow()
            await db_objects.update(payload_type)
            # now to process all of the files associated with the payload type
            #    make all of the necessary folders for us first
            os.makedirs("./app/payloads/{}".format(payload_type.ptype), exist_ok=True)  # make the directory structure
            os.makedirs("./app/payloads/{}/payload".format(payload_type.ptype), exist_ok=True)  # make the directory structure
            os.makedirs("./app/payloads/{}/commands".format(payload_type.ptype), exist_ok=True)  # make the directory structure
            abs_payload_path = os.path.abspath("./app/payloads/{}/payload/".format(payload_type.ptype))
            for payload_file in ptype['files']:
                for file_name in payload_file:  # {"filename.extension": "base64 blob"}
                    file_name_path = os.path.abspath("./app/payloads/{}/payload/{}".format(payload_type.ptype, file_name))
                    if abs_payload_path in file_name_path:
                        os.makedirs(os.path.dirname(file_name_path), exist_ok=True)  # make sure all  directories exist first
                        ptype_file = open(file_name_path, 'wb')
                        ptype_content = base64.b64decode(payload_file[file_name])
                        ptype_file.write(ptype_content)
                        ptype_file.close()
            # now to process the transforms
            # create any TransformCode entries as needed,just keep track of them
            transforms = {}
            for tc in ptype['transforms']:
                try:
                    tc_entry = await db_objects.get(db_model.TransformCode, name=tc['name'])
                except Exception as e:
                    tc_entry = await db_objects.create(db_model.TransformCode, name=tc['name'], parameter_type=tc['parameter_type'],
                                                       description=tc['description'], operator=operator,
                                                       code=tc['code'], is_command_code=tc['is_command_code'])
                transforms[tc['name']] = tc_entry
            for lt in ptype['load_transforms']:
                try:
                    query = await db_model.transform_query()
                    cmd_lt = await db_objects.get(query, payload_type=payload_type, t_type="load",
                                                  order=lt['order'], transform=transforms[lt['transform']])
                    cmd_lt.parameter = lt['parameter']
                    cmd_lt.description = lt['description']
                    cmd_lt.operator = operator
                    await db_objects.update(cmd_lt)
                except Exception as e:
                    await db_objects.create(Transform, payload_type=payload_type, transform=transforms[lt['transform']],
                                            t_type="load", operator=operator, parameter=lt['parameter'],
                                            order=lt['order'], description=lt['description'])
            for ct in ptype['create_transforms']:
                try:
                    query = await db_model.transform_query()
                    cmd_ct = await db_objects.get(query, payload_type=payload_type, t_type="create",
                                                  order=ct['order'], transform=transforms[ct['transform']])
                    cmd_ct.parameter = ct['parameter']
                    cmd_ct.description = ct['description']
                    cmd_ct.operator = operator
                    await db_objects.update(cmd_ct)
                except Exception as e:
                    await db_objects.create(Transform, payload_type=payload_type, transform=transforms[ct['transform']],
                                            t_type="create", operator=operator, parameter=ct['parameter'],
                                            order=ct['order'], description=ct['description'])
            # go through support scripts and add as necessary
            if 'support_scripts' in ptype:
                browserscriptquery = await db_model.browserscript_query()
                for support_script in ptype['support_scripts']:
                    try:
                        script = await db_objects.get(browserscriptquery, name=support_script['name'], operator=operator)
                        script.script = support_script['script']
                    except Exception as e:
                        await db_objects.create(db_model.BrowserScript, name=support_script['name'], operator=operator,
                                                script=support_script['script'])
            # now that we have the payload type, start processing the commands and their parts
            await import_command_func(payload_type, operator, ptype['commands'], transforms)
            if 'c2_profiles' in ptype:
                for c2_profile_name in ptype['c2_profiles']:  # {"default": [{"default.h": "base64"}, {"default.c": "base64"} ]}, {"RESTful Patchtrhough": []}
                    # make sure this c2 profile exists for this operation first
                    try:
                        query = await db_model.c2profile_query()
                        c2_profile = await db_objects.get(query, name=c2_profile_name)
                        query = await db_model.payloadtypec2profile_query()
                        try:
                            await db_objects.get(query, payload_type=payload_type, c2_profile=c2_profile)
                        except Exception as e:
                            # it doesn't exist, so we create it
                            await db_objects.create(PayloadTypeC2Profile, payload_type=payload_type,
                                                    c2_profile=c2_profile)
                    except Exception as e:
                        print("Failed to associated profile with payload type")
                        continue  # just try to get the next c2_profile
                    # now deal with the files
                    os.makedirs("./app/c2_profiles/{}/{}".format(c2_profile_name, ptype['ptype']), exist_ok=True)
                    for c2_file in ptype['c2_profiles'][c2_profile_name]:  # list of files
                        # associate the new payload type with this C2 profile and create directory as needed
                        for c2_file_name in c2_file:
                            ptype_file = open(
                                "./app/c2_profiles/{}/{}/{}".format(c2_profile_name, ptype['ptype'], c2_file_name), 'wb')
                            ptype_content = base64.b64decode(c2_file[c2_file_name])
                            ptype_file.write(ptype_content)
                            ptype_file.close()
            await write_transforms_to_file()
            await update_all_pt_transform_code()
            return {'status': 'success'}
        except Exception as e:
            logger.exception("exception on importing payload type")
            return {'status': 'error',  'error': str(e)}
    except Exception as e:
        logger.exception("failed to import a payload type: " + str(e))
        return {'status': 'error',  'error': str(e)}


async def import_command_func(payload_type, operator, command_list, transforms):
    for cmd in command_list:
        if 'is_exit' not in cmd:
            cmd['is_exit'] = False
        elif cmd['is_exit'] is True:
            # this is trying to say it is the exit command for this payload type
            # there can only be one for a given payload type though, so check. if one exists, change it
            query = await db_model.command_query()
            try:
                exit_command = await db_objects.get(
                    query.where((Command.is_exit == True) & (Command.payload_type == payload_type) & (Command.deleted == False)))
                # one is already set, so set it to false
                exit_command.is_exit = False
                await db_objects.update(exit_command)
            except Exception as e:
                # one doesn't exist, so let this one be set
                pass
        if 'is_process_list' not in cmd:
            cmd['is_process_list'] = False
            cmd['process_list_parameters'] = ""
        elif cmd['is_process_list'] is True:
            query = await db_model.command_query()
            try:
                pl_command = await db_objects.get(
                    query.where((Command.is_process_list == True) & (Command.payload_type == payload_type) & (Command.deleted == False)))
                # one is already set, so set it to false
                pl_command.is_process_list = False
                await db_objects.update(pl_command)
            except Exception as e:
                # one doesn't exist, so let this one be set
                pass
            cmd['process_list_parameters'] = cmd['process_list_parameters'] if 'process_list_parameters' in cmd else ""
        if 'is_file_browse' not in cmd:
            cmd['is_file_browse'] = False
            cmd['file_browse_parameters'] = ""
        elif cmd['is_file_browse'] is True:
            query = await db_model.command_query()
            try:
                fb_command = await db_objects.get(
                    query.where((Command.is_file_browse == True) & (Command.payload_type == payload_type) & (Command.deleted == False)))
                # one is already set, so set it to false
                fb_command.is_file_browse = False
                await db_objects.update(fb_command)
            except Exception as e:
                # one doesn't exist, so let this one be set
                pass
            cmd['file_browse_parameters'] = cmd['file_browse_parameters'] if 'file_browse_parameters' in cmd else "*"
        if 'is_download_file' not in cmd:
            cmd['is_download_file'] = False
            cmd['download_file_parameters'] = ""
        elif cmd['is_download_file'] is True:
            query = await db_model.command_query()
            try:
                df_command = await db_objects.get(
                    query.where((Command.is_download_file == True) & (Command.payload_type == payload_type) & (Command.deleted == False)))
                # one is already set, so set it to false
                df_command.is_download_file = False
                await db_objects.update(df_command)
            except Exception as e:
                # one doesn't exist, so let this one be set
                pass
            cmd['download_file_parameters'] = cmd[
                'download_file_parameters'] if 'download_file_parameters' in cmd else "*"
        if 'is_remove_file' not in cmd:
            cmd['is_remove_file'] = False
            cmd['remove_file_parameters'] = ""
        elif cmd['is_remove_file'] is True:
            query = await db_model.command_query()
            try:
                rf_command = await db_objects.get(
                    query.where((Command.is_remove_file == True) & (Command.payload_type == payload_type) & (Command.deleted == False)))
                # one is already set, so set it to false
                rf_command.is_remove_file = False
                await db_objects.update(rf_command)
            except Exception as e:
                # one doesn't exist, so let this one be set
                pass
            cmd['remove_file_parameters'] = cmd['remove_file_parameters'] if 'remove_file_parameters' in cmd else "*"
        if 'is_agent_generator' not in cmd:
            cmd['is_agent_generator'] = False
        try:
            query = await db_model.command_query()
            command = await db_objects.get(query, cmd=cmd['cmd'], payload_type=payload_type)
            command.description = cmd['description']
            command.needs_admin = cmd['needs_admin']
            command.version = cmd['version']
            command.help_cmd = cmd['help_cmd']
            command.is_exit = cmd['is_exit']
            command.is_process_list = cmd['is_process_list']
            command.process_list_parameters = cmd['process_list_parameters']
            command.is_file_browse = cmd['is_file_browse']
            command.file_browse_parameters = cmd['file_browse_parameters']
            command.is_download_file = cmd['is_download_file']
            command.download_file_parameters = cmd['download_file_parameters']
            command.is_remove_file = cmd['is_remove_file']
            command.remove_file_parameters = cmd['remove_file_parameters']
            command.is_agent_generator = cmd['is_agent_generator']
            command.operator = operator
            await db_objects.update(command)
        except Exception as e:  # this means that the command doesn't already exist
            command = await db_objects.create(Command, cmd=cmd['cmd'], payload_type=payload_type,
                                              description=cmd['description'], version=cmd['version'],
                                              needs_admin=cmd['needs_admin'], help_cmd=cmd['help_cmd'],
                                              operator=operator, is_exit=cmd['is_exit'],
                                              is_process_list=cmd['is_process_list'],
                                              process_list_parameters=cmd['process_list_parameters'],
                                              is_file_browse=cmd['is_file_browse'],
                                              file_browse_parameters=cmd['file_browse_parameters'],
                                              is_download_file=cmd['is_download_file'],
                                              download_file_parameters=cmd['download_file_parameters'],
                                              is_remove_file=cmd['is_remove_file'],
                                              remove_file_parameters=cmd['remove_file_parameters'],
                                              is_agent_generator=cmd['is_agent_generator'])
        if 'transforms' in cmd:
            query = await db_model.commandtransform_query()
            for transform in cmd['transforms']:
                try:
                    cmd_transform = await db_objects.get(query, command=command, transform=transforms[transform['transform']],
                                                         order=transform['order'], parameter=transform['parameter'])
                    cmd_transform.active = True
                    cmd_transform.description = transform['description']
                    cmd_transform.operator = operator
                    await db_objects.update(cmd_transform)
                except Exception as e:
                    cmd_transform = await db_objects.create(db_model.CommandTransform, command=command,
                                                            transform=transforms[transform['transform']],
                                                            order=transform['order'], parameter=transform['parameter'],
                                                            active=True, description=transform['description'],
                                                            operator=operator)
                #print(cmd_transform.to_json())
        # now to process the parameters
        for param in cmd['parameters']:
            try:
                query = await db_model.commandparameters_query()
                cmd_param = await db_objects.get(query, command=command, name=param['name'])
                cmd_param.type = param['type']
                cmd_param.hint = param['hint']
                cmd_param.choices = param['choices']
                cmd_param.required = param['required']
                cmd_param.operator = operator
                await db_objects.update(cmd_param)
            except:  # param doesn't exist yet, so create it
                await db_objects.create(CommandParameters, command=command, operator=operator, **param)
        # now to process the att&cks
        for attack in cmd['attack']:
            query = await db_model.attack_query()
            attck = await db_objects.get(query, t_num=attack['t_num'])
            query = await db_model.attackcommand_query()
            try:
                await db_objects.get(query, command=command, attack=attck)
            except Exception as e:
                # we got here so it doesn't exist, so create it and move on
                await db_objects.create(ATTACKCommand, command=command, attack=attck)
        # now to process the artifacts
        for at in cmd['artifacts']:
            try:
                query = await db_model.artifact_query()
                artifacttemplatequery = await db_model.artifacttemplate_query()
                # first try to get the base artifact, if it doesn't exist, make it
                try:
                    artifact = await db_objects.get(
                        query.where(fn.encode(db_model.Artifact.name, 'escape') == at['artifact']))
                except Exception as e:
                    artifact = await db_objects.create(db_model.Artifact, name=at['artifact'], description="created during payload import")
                # now try to get the artifacttemplate. if it doesn't exist, make it
                try:
                    artifact_template = await db_objects.get(artifacttemplatequery, command=command, artifact=artifact,
                                                             artifact_string=at['artifact_string'],
                                                             replace_string=at['replace_string'])
                except Exception as e:
                    artifact_template = await db_objects.create(ArtifactTemplate, command=command, artifact=artifact,
                                                                artifact_string=at['artifact_string'],
                                                                replace_string=at['replace_string'])
                if at['command_parameter'] is not None and at['command_parameter'] != "null":
                    query = await db_model.commandparameters_query()
                    command_parameter = await db_objects.get(query, command=command, name=at['command_parameter'])
                    artifact_template.command_parameter = command_parameter
                    await db_objects.update(artifact_template)
            except Exception as e:
                logger.exception("Failed to find artifact template: " + at['artifact'])
                print("failed to import artifact template due to missing base artifact")
        # now process the command file
        os.makedirs("./app/payloads/{}/commands/{}/".format(payload_type.ptype, command.cmd), exist_ok=True)
        if 'files' in cmd:
            for cmd_entry in cmd['files']:
                for file_name in cmd_entry:  # {filename: base64 file}
                    file_name_path = "./app/payloads/{}/commands/{}/{}".format(payload_type.ptype, command.cmd, file_name)
                    os.makedirs(os.path.dirname(file_name_path),  exist_ok=True)  # make sure all  directories exist first
                    cmd_file = open(file_name_path, 'wb')
                    cmd_file_data = base64.b64decode(cmd_entry[file_name])
                    cmd_file.write(cmd_file_data)
                    cmd_file.close()
        if 'browser_script' in cmd:
            try:
                query = await db_model.browserscript_query()
                script = await db_objects.get(query, command=command, operator=operator)
                script.script = cmd['browser_script']
            except Exception as e:
                await db_objects.create(db_model.BrowserScript, command=command, operator=operator,
                                        script=cmd['browser_script'])


@apfell.route(apfell.config['API_BASE'] + "/payloadtypes/import/commands", methods=['POST'])
@inject_user()
@scoped(['auth:user', 'auth:apitoken_user'], False)  # user or user-level api token are ok
async def import_commands(request, user):
    if user['auth'] not in ['access_token', 'apitoken']:
        abort(status_code=403, message="Cannot access via Cookies. Use CLI or access via JS in browser")
    if request.files:
        try:
            data = js.loads(request.files['upload_file'][0].body.decode('UTF-8'))
        except Exception as e:
            logger.exception("Failed to parse uploaded json file for importing commands")
            return json({'status': 'error', 'error': 'failed to parse file'})
    else:
        try:
            data = request.json
        except Exception as e:
            logger.exception("exception in parsing JSON when importing commands")
            return json({'status': 'error', 'error': 'failed to parse JSON'})
    if "payload_types" not in data:
        return json({'status': 'error', 'error': 'must start with "payload_types"'})
    try:
        query = await db_model.operator_query()
        operator = await db_objects.get(query, username=user['username'])
        query = await db_model.operation_query()
        operation = await db_objects.get(query, name=user['current_operation'])
        query = await db_model.payloadtype_query()
    except Exception as e:
        logger.exception("Failed to get operator or current operation")
        return json({'status': 'error', 'error': 'failed to get operator or current_operation information'})
    for e in data['payload_types']:
        for ptype in e:
            try:
                payload_type = await db_objects.get(query, ptype=ptype)
                await import_command_func(payload_type, operator, e[ptype])
            except Exception as e:
                logger.exception("Failed to find payload type: " + ptype)
                continue
    return json({'status': 'success'})