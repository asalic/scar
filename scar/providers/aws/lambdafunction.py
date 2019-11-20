# Copyright (C) GRyCAP - I3M - UPV
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import json
from multiprocessing.pool import ThreadPool
from botocore.exceptions import ClientError
from scar.providers.aws import GenericClient
from scar.providers.aws.functioncode import FunctionPackager
from scar.providers.aws.lambdalayers import LambdaLayers
from scar.providers.aws.clients.lambdafunction import LambdaClient
from scar.providers.aws.s3 import S3
from scar.providers.aws.validators import AWSValidator
import scar.exceptions as excp
import scar.http.request as request
import scar.logger as logger
import scar.providers.aws.response as response_parser
from scar.utils import DataTypesUtils, FileUtils, StrUtils
from typing import Dict

MAX_CONCURRENT_INVOCATIONS = 500
ASYNCHRONOUS_CALL = {"invocation_type": "Event",
                     "log_type": "None",
                     "asynchronous": "True"}
REQUEST_RESPONSE_CALL = {"invocation_type": "RequestResponse",
                         "log_type": "Tail",
                         "asynchronous": "False"}


def _get_layers_client(client: LambdaClient, supervisor_version: str) -> LambdaLayers:
    return LambdaLayers(client, supervisor_version)


def _get_s3_client(aws_properties: Dict) -> S3:
    return S3(aws_properties)


class Lambda(GenericClient):

    def __init__(self, aws_properties: Dict) -> None:
        super().__init__(aws_properties.get('lambda', {}))
        self._aws = aws_properties
        self.function = aws_properties.get('lambda', {})
        self.tmp_folder = FileUtils.create_tmp_dir()
        self.zip_payload_path = FileUtils.join_paths(self.tmp_folder.name, 'function.zip')

    def _get_creations_args(self):
        return {'FunctionName': self.function.get('name'),
                'Runtime': self.function.get('runtime'),
                'Role': self._aws.get('iam').get('role'),
                'Handler': self.function.get('handler'),
                'Code': self._get_function_code(),
                'Environment': self.function.get('environment'),
                'Description': self.function.get('description'),
                'Timeout':  self.function.get('timeout'),
                'MemorySize': self.function.get('memory'),
                'Tags': self.function.get('tags'),
                'Layers': self.function.get('layers')}

    def is_asynchronous(self):
        return self.function.get('asynchronous', False)

    def get_access_key(self) -> str:
        """Returns the access key belonging to the boto_profile used."""
        return self.client.get_access_key()

    @excp.exception(logger)
    def create_function(self):
        self._manage_supervisor_layer()
        creation_args = self._get_creations_args()
        response = self.client.create_function(**creation_args)
        if response and "FunctionArn" in response:
            self.function['arn'] = response.get('FunctionArn', "")
        return response

    def _manage_supervisor_layer(self):
        layers_client = LambdaLayers(self.function, self.client)
        layers_client.check_faas_supervisor_layer()
        self.function.get('layers', []).append(layers_client.get_latest_supervisor_layer_arn())

    @excp.exception(logger)
    def _get_function_code(self):
        # Zip all the files and folders needed
        code = {}
        FunctionPackager(self._aws).create_zip(self.zip_payload_path)
        if self.function.get('deployment').get('bucket', False):
            file_key = f"lambda/{self.function.get('name')}.zip"
            self._get_s3_client().upload_file(file_path=self.zip_payload_path,
                                              file_key=file_key)
            code = {"S3Bucket": self.function.get('deployment_bucket'),
                    "S3Key": file_key}
        else:
            code = {"ZipFile": FileUtils.read_file(self.zip_payload_path, mode="rb")}
        return code

    def delete_function(self, function_name):
        return self.client.delete_function(function_name)

    def link_function_and_input_bucket(self):
        kwargs = {'FunctionName' : self._aws.lambdaf.name,
                  'Principal' : "s3.amazonaws.com",
                  'SourceArn' : 'arn:_aws:s3:::{0}'.format(self._aws.s3.input_bucket)}
        self.client.add_invocation_permission(**kwargs)

    def preheat_function(self):
        logger.info("Preheating function")
        self._set_request_response_call_parameters()
        return self.launch_lambda_instance()

    def _launch_async_event(self, s3_event):
        self.set_asynchronous_call_parameters()
        return self._launch_s3_event(s3_event)

    def launch_request_response_event(self, s3_event):
        self._set_request_response_call_parameters()
        return self._launch_s3_event(s3_event)

    def _launch_s3_event(self, s3_event):
        self._aws.lambdaf.payload = s3_event
        logger.info(f"Sending event for file '{s3_event['Records'][0]['s3']['object']['key']}'")
        return self.launch_lambda_instance()

    def process_asynchronous_lambda_invocations(self, s3_event_list):
        if (len(s3_event_list) > MAX_CONCURRENT_INVOCATIONS):
            s3_file_chunk_list = DataTypesUtils.divide_list_in_chunks(s3_event_list, MAX_CONCURRENT_INVOCATIONS)
            for s3_file_chunk in s3_file_chunk_list:
                self._launch_concurrent_lambda_invocations(s3_file_chunk)
        else:
            self._launch_concurrent_lambda_invocations(s3_event_list)

    def _launch_concurrent_lambda_invocations(self, s3_event_list):
        pool = ThreadPool(processes=len(s3_event_list))
        pool.map(lambda s3_event: self._launch_async_event(s3_event), s3_event_list)
        pool.close()

    def launch_lambda_instance(self):
        response = self._invoke_lambda_function()
        response_args = {'Response' : response,
                         'FunctionName' : self._aws.lambdaf.name,
                         'OutputType' : self._aws.output,
                         'IsAsynchronous' : self._aws.lambdaf.asynchronous}
        if hasattr(self._aws, "output_file"):
            response_args['OutputFile'] = self._aws.output_file
        response_parser.parse_invocation_response(**response_args)

    def _get_invocation_payload(self):
        # Default payload
        payload = self.function.get('payload', {})
        if not payload:
            # Check for defined run script
            if self.function.get("run_script", False):
                script_path = self.function.get("run_script")
                # We first code to base64 in bytes and then decode those bytes to allow the json lib to parse the data
                # https://stackoverflow.com/questions/37225035/serialize-in-json-a-base64-encoded-data#37239382
                payload = { "script" : StrUtils.bytes_to_base64str(FileUtils.read_file(script_path, 'rb')) }
            # Check for defined commands
            # This overrides any other function payload
            if self.function.get("c_args", False):
                payload = {"cmd_args" : json.dumps(self.function.get("c_args"))}
        return json.dumps(payload)

    def _invoke_lambda_function(self):
        invoke_args = {'FunctionName' :  self.function.get('name'),
                       'InvocationType' :  self.function.get('invocation_type'),
                       'LogType' :  self.function.get('log_type'),
                       'Payload' : self._get_invocation_payload()}
        return self.client.invoke_function(**invoke_args)

    def set_asynchronous_call_parameters(self):
        self.function.update(ASYNCHRONOUS_CALL)

    def _set_request_response_call_parameters(self):
        self.function.update(REQUEST_RESPONSE_CALL)

    def _update_environment_variables(self, function_info, update_args):
        # To update the environment variables we need to retrieve the
        # variables defined in lambda and update them with the new values
        env_vars = self._aws.lambdaf.environment
        if hasattr(self._aws.lambdaf, "environment_variables"):
            for env_var in self._aws.lambdaf.environment_variables:
                key_val = env_var.split("=")
                # Add an specific prefix to be able to find the variables defined by the user
                env_vars['Variables']['CONT_VAR_{0}'.format(key_val[0])] = key_val[1]
        if hasattr(self._aws.lambdaf, "timeout_threshold"):
            env_vars['Variables']['TIMEOUT_THRESHOLD'] = str(self._aws.lambdaf.timeout_threshold)
        if hasattr(self._aws.lambdaf, "log_level"):
            env_vars['Variables']['LOG_LEVEL'] = self._aws.lambdaf.log_level
        function_info['Environment']['Variables'].update(env_vars['Variables'])
        update_args['Environment'] = function_info['Environment']

    def _update_supervisor_layer(self, function_info, update_args):
        if hasattr(self._aws.lambdaf, "supervisor_layer"):
            # Set supervisor layer Arn
            function_layers = [self.layers.get_latest_supervisor_layer_arn()]
            # Add the rest of layers (if exist)
            if 'Layers' in function_info:
                function_layers.extend([layer for layer in function_info['Layers'] if self.layers.layer_name not in layer['Arn']])
            update_args['Layers'] = function_layers

    def update_function_configuration(self, function_info=None):
        if not function_info:
            function_info = self.get_function_info()
        update_args = {'FunctionName' : function_info['FunctionName'] }
#         if hasattr(self._aws.lambdaf, "memory"):
#             update_args['MemorySize'] = self._aws.lambdaf.memory
#         else:
#             update_args['MemorySize'] = function_info['MemorySize']
#         if hasattr(self._aws.lambdaf, "time"):
#             update_args['Timeout'] = self._aws.lambdaf.time
#         else:
#             update_args['Timeout'] = function_info['Timeout']
        self._update_environment_variables(function_info, update_args)
        self._update_supervisor_layer(function_info, update_args)
        self.client.update_function_configuration(**update_args)
        logger.info("Function '{}' updated successfully.".format(function_info['FunctionName']))

    def _get_function_environment_variables(self):
        return self.get_function_info()['Environment']

    def get_all_functions(self, arn_list):
        try:
            return [self.get_function_info(function_arn) for function_arn in arn_list]
        except ClientError as cerr:
            print (f"Error getting function info by arn: {cerr}")

    def get_function_info(self, function_name_or_arn=None):
        name_arn = function_name_or_arn if function_name_or_arn else self._aws.lambdaf.name
        return self.client.get_function_info(name_arn)

    @excp.exception(logger)
    def find_function(self, function_name_or_arn=None):
        try:
            # If this call works the function exists
            name_arn = function_name_or_arn if function_name_or_arn else self.function.get('name', '')
            self.get_function_info(name_arn)
            return True
        except ClientError as ce:
            # Function not found
            if ce.response['Error']['Code'] == 'ResourceNotFoundException':
                return False
            else:
                raise

    def add_invocation_permission_from_api_gateway(self):
        kwargs = {'FunctionName' : self._aws.lambdaf.name,
                  'Principal' : 'apigateway.amazonaws.com',
                  'SourceArn' : 'arn:_aws:execute-api:{0}:{1}:{2}/*'.format(self._aws.region,
                                                                           self._aws.account_id,
                                                                           self._aws.api_gateway.id)}
        # Add Testing permission
        self.client.add_invocation_permission(**kwargs)
        # Add Invocation permission
        kwargs['SourceArn'] = 'arn:_aws:execute-api:{0}:{1}:{2}/scar/ANY'.format(self._aws.region,
                                                                                self._aws.account_id,
                                                                                self._aws.api_gateway.id)
        self.client.add_invocation_permission(**kwargs)

    def get_api_gateway_id(self):
        env_vars = self._get_function_environment_variables()
        return env_vars['Variables'].get('API_GATEWAY_ID', '')

    def _get_api_gateway_url(self):
        api_id = self.get_api_gateway_id()
        if not api_id:
            raise excp.ApiEndpointNotFoundError(self._aws.lambdaf.name)
        return f'https://{api_id}.execute-api.{self._aws.region}.amazonaws.com/scar/launch'

    def call_http_endpoint(self):
        invoke_args = {'headers' : {'X-Amz-Invocation-Type':'Event'} if self.is_asynchronous() else {}}
        if hasattr(self._aws, "api_gateway"):
            self._set_invoke_args(invoke_args)
        return request.call_http_endpoint(self._get_api_gateway_url(), **invoke_args)

    def _set_invoke_args(self, invoke_args):
        if hasattr(self._aws.api_gateway, "data_binary"):
            invoke_args['data'] = self._get_b64encoded_binary_data(self._aws.api_gateway.data_binary)
            invoke_args['headers'] = {'Content-Type': 'application/octet-stream'}
        if hasattr(self._aws.api_gateway, "parameters"):
            invoke_args['params'] = self._parse_http_parameters(self._aws.api_gateway.parameters)
        if hasattr(self._aws.api_gateway, "json_data"):
            invoke_args['data'] = self._parse_http_parameters(self._aws.api_gateway.json_data)
            invoke_args['headers'] = {'Content-Type': 'application/json'}

    def _parse_http_parameters(self, parameters):
        return parameters if type(parameters) is dict else json.loads(parameters)

    @excp.exception(logger)
    def _get_b64encoded_binary_data(self, data_path):
        if data_path:
            AWSValidator.validate_http_payload_size(data_path, self.is_asynchronous())
            with open(data_path, 'rb') as data_file:
                return base64.b64encode(data_file.read())
