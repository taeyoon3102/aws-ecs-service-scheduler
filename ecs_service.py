# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0


import copy
import re

from instance_scheduler import schedulers
import re
import copy
from botocore.exceptions import ClientError

from instance_scheduler.boto_retry import get_client_with_standard_retry
from instance_scheduler.configuration.instance_schedule import InstanceSchedule
from instance_scheduler.configuration.running_period import RunningPeriod
from instance_scheduler.configuration.scheduler_config_builder import (
    SchedulerConfigBuilder,
)
from instance_scheduler.configuration.setbuilders.weekday_setbuilder import (
    WeekdaySetBuilder,
)

RESTRICTED_ECS_TAG_VALUE_SET_CHARACTERS = r"[^a-zA-Z0-9\s_\.:+/=\\@-]"

INF_ADD_TAGS = "Adding {} tags {} to instance {}"
INF_REMOVE_KEYS = "Removing {} key(s) {} from instance {}"
INF_FETCHING_RESOURCES = "Fetching ecs {} for account {} in region {}"


DEBUG_READ_REPLICA = (
    'Can not schedule ecs instance "{}" because it is a read replica of instance {}'
)

DEBUG_SKIPPING_INSTANCE = (
    "Skipping ecs {} {} because it is not in a start or stop-able state ({})"
)
DEBUG_WITHOUT_SCHEDULE = "Skipping ecs {} {} without schedule"
DEBUG_SELECTED = "Selected ecs instance {} in state ({}) for schedule {}"
DEBUG_NO_SCHEDULE_TAG = "Instance {} has no schedule tag named {}"

WARN_TAGGING_STARTED = "Error setting start or stop tags to started instance {}, ({})"
WARN_TAGGING_STOPPED = "Error setting start or stop tags to stopped instance {}, ({})"
WARN_ECS_TAG_VALUE = (
    'Tag value "{}" for tag "{}" changed to "{}" because it did contain characters that are not allowed '
    "in ECS tag values. The value can only contain only the set of Unicode letters, digits, "
    "white-space, '_', '.', '/', '=', '+', '-'"
)




class EcsService:
    ECS_STATE_RUNNING = "running"
    ECS_STATE_STOPPED = "stopped"

    ECS_SCHEDULABLE_STATES = {ECS_STATE_RUNNING, ECS_STATE_STOPPED}

    def __init__(self):
        self.service_name = "ecs"
        self.allow_resize = False
        self._instance_tags = None

        self._context = None
        self._session = None
        self._region = None
        self._account = None
        self._logger = None
        self._tagname = None
        self._stack_name = None
        self._config = None

    def _init_scheduler(self, args):
        """
        Initializes common parameters
        :param args: action parameters
        :return:
        """
        self._account = args.get(schedulers.PARAM_ACCOUNT)
        self._context = args.get(schedulers.PARAM_CONTEXT)
        self._logger = args.get(schedulers.PARAM_LOGGER)
        self._region = args.get(schedulers.PARAM_REGION)
        self._stack_name = args.get(schedulers.PARAM_STACK)
        self._session = args.get(schedulers.PARAM_SESSION)
        self._tagname = args.get(schedulers.PARAM_CONFIG).tag_name
        self._config = args.get(schedulers.PARAM_CONFIG)
        self._instance_tags = None

   

    def get_schedulable_resources(self, fn_is_schedulable, fn_describe_name, kwargs):
        self._init_scheduler(kwargs)

        client = get_client_with_standard_retry(
            "ecs", session=self._session, region=self._region
        )
        
        resource_name = fn_describe_name.split("_")[-1]
        resource_name = resource_name[0].upper() + resource_name[1:]
        
        args = {}
        resources = [] ## service
        done = False
        self._logger.info(
            INF_FETCHING_RESOURCES, resource_name, self._account, self._region
        )
        
        clusters = []
        
        while not done:
            ecs_resp = client.list_clusters(**args)
            clusterArns = []
            if "clusterArns" in ecs_resp : 
                clusterArns = ecs_resp["clusterArns"]
                for clusterArn in clusterArns :
                    cluster_name = clusterArn.split("/")
                    clusters.append(cluster_name[1])
                    number_of_clusters =+ 1
          
            if "NextToken" in ecs_resp:
                args["NextToken"] = ecs_resp["NextToken"]
            else:
                done = True
        
        
        args = {}
        cluster_service = [] # list of cluster-service pair info
        
        for cluster in clusters:
            done = False
            while not done:

                ecs_resp = client.list_services(cluster = cluster)
                serviceArns = []
                if "serviceArns" in ecs_resp : 
                    serviceArns = ecs_resp["serviceArns"]
                    for serviceArn in serviceArns :
                        service_name = serviceArn.split("/")
                        cluster_service.append({"cluster":cluster, "service":[service_name[2]]})
              
                if "NextToken" in ecs_resp:
                    args["NextToken"] = ecs_resp["NextToken"]
                else:
                    done = True

        
        # describe service using cluster-service pair        
        for service in cluster_service:
            done = False
            while not done:
                ecs_resp = client.describe_services(cluster=service["cluster"], services=service["service"], include=['TAGS',])
                service_data=ecs_resp["services"][0]

                resource = self._select_service_data(
                    service=service_data, tagname=self._tagname, config=self._config
                )
                
                if resource != {}:
                    resources.append(resource)
                
                
                if "NextToken" in ecs_resp:
                    args["NextToken"] = ecs_resp["NextToken"]
                else:
                    done = True

        return resources

    def get_schedulable_ecs_instances(self, kwargs):
        def is_schedulable_instance(ecs_inst):
            return True

        return self.get_schedulable_resources(
            fn_is_schedulable=is_schedulable_instance,
            fn_describe_name="list-clusters",
            kwargs=kwargs,
        )



    def get_schedulable_instances(self, kwargs):
        instances = self.get_schedulable_ecs_instances(kwargs)

        return instances


    def _select_service_data(self, service, tagname, config):
        def get_tags(inst):
            return (
                {tag["key"]: tag["value"] for tag in inst["tags"]}
                if "tags" in inst
                else {}
            )
        
        tags = get_tags(service)
        if tags == {} :
            return {}
        
        schedule_name = tags.get(tagname)
        tag_desired_count = tags.get("desiredCount")

        if schedule_name is None:
            return {}
    
        desiredCount = service["desiredCount"]
        serviceArn = service["serviceArn"]
        serviceName = service["serviceName"]
        clusterName = (service["clusterArn"].split("/"))[1]
        state = service["status"]
        if desiredCount == 0 :
            state = "stopped"
            is_running = False
        else :
            state = "running"
            is_running = True

        instanceType = service["launchType"]

        service_data = {
            schedulers.INST_HIBERNATE: False,
            schedulers.INST_IS_RUNNING: is_running,
            schedulers.INST_MAINTENANCE_WINDOW: None,
            schedulers.INST_INSTANCE_TYPE: instanceType,
            schedulers.INST_CURRENT_STATE: state,
            schedulers.INST_SCHEDULE: schedule_name,
            schedulers.INST_STATE: state,
            schedulers.INST_STATE_NAME: state,
            schedulers.INST_ALLOW_RESIZE: self.allow_resize,
            schedulers.INST_ID: serviceName,
            schedulers.INST_ARN: serviceArn,
            schedulers.INST_TAGS: tags,
            schedulers.INST_NAME: serviceName,
            schedulers.INST_IS_TERMINATED: False,
            "desiredCount": desiredCount, ## current desiredCount value
            "tagDesiredCount": tag_desired_count, ## saved desiredCount value in tags
            "serviceName": serviceName,
            "clusterName": clusterName,
            "serviceArn": serviceArn,
        }

        return service_data


    def resize_instance(self, kwargs):
        pass

    def _validate_ecs_tag_values(self, tags):
        result = copy.deepcopy(tags)
        for t in result:
            original_value = t.get("Value", "")
            value = re.sub(RESTRICTED_ECS_TAG_VALUE_SET_CHARACTERS, " ", original_value)
            value = value.replace("\n", " ")
            if value != original_value:
                self._logger.warning(WARN_ECS_TAG_VALUE, original_value, t, value)
                t["Value"] = value
        return result

    # # noinspection PyMethodMayBeStatic
    def stop_instances(self, kwargs):
        self._init_scheduler(kwargs)
        client = get_client_with_standard_retry(
            "ecs", session=self._session, region=self._region
        )
        stopped_instances = kwargs[schedulers.PARAM_STOPPED_INSTANCES]

        for ecs_service in stopped_instances:
            try:
                if ecs_service.desiredCount != 0:
                    resp = client.update_service(cluster=ecs_service.clusterName, service=ecs_service.serviceName, desiredCount=0)
                    tag_resp = client.tag_resource(resourceArn=ecs_service.serviceArn, tags = [{"key":"desiredCount", "value":str(ecs_service.desiredCount)}])
                    self._tag_stopped_resource(client, ecs_service)
                    yield ecs_service.id, "stopped"
            except ClientError as ex:     
                self._logger.error(
                    "ecs_service stopped_instances ClientError: {}", ex
                )
        


    # # noinspection PyMethodMayBeStatic
    def start_instances(self, kwargs):

        self._init_scheduler(kwargs)
        
        client = get_client_with_standard_retry(
            "ecs", session=self._session, region=self._region
        )
        
        started_instances = kwargs[schedulers.PARAM_STARTED_INSTANCES]


        for ecs_service in started_instances:
            try:
                if ecs_service.desiredCount == 0:
                    if ecs_service.tagDesiredCount == None :
                        restart_desicredCount = 1 # set default count 1
                        self._logger.error(
                           "No desiredCount in ecs service tags, so set default count 1"
                        )
                    else :
                        restart_desicredCount = ecs_service.tagDesiredCount
                    resp = client.update_service(cluster=ecs_service.clusterName, service=ecs_service.serviceName, desiredCount=int(restart_desicredCount))
                    untag_resp = client.untag_resource(resourceArn=ecs_service.serviceArn, tagKeys=["desiredCount"])
                    self._tag_started_resource(client, ecs_service)

                    yield ecs_service.id, "running"
            except ClientError as ex:     
                self._logger.error(
                    "ecs_service started_instances ClientError: {}", ex
                )


    def _tag_stopped_resource(self, client, ecs_resource):
        stop_tags = self._validate_ecs_tag_values(self._config.stopped_tags)
        if stop_tags is None:
            stop_tags = []
        stop_tags_key_names = [t["Key"] for t in stop_tags]
        start_tags_keys = [
            t["Key"]
            for t in self._config.started_tags
            if t["Key"] not in stop_tags_key_names
        ]
        try:
            if start_tags_keys is not None and len(start_tags_keys):
                self._logger.info(
                    INF_REMOVE_KEYS,
                    "start",
                    ",".join(['"{}"'.format(k) for k in start_tags_keys]),
                    ecs_resource.arn,
                )
                client.untag_resource(
                    resourceArn=ecs_resource.arn, tags=start_tags_keys
                )
            if len(stop_tags) > 0:
                self._logger.info(
                    INF_ADD_TAGS, "stop", str(stop_tags), ecs_resource.arn
                )
                new_stop_tags = []
                for t in stop_tags:
                    new_stop_tags.append({"key":t["Key"], "value":t["Value"]})
                client.tag_resource(
                    resourceArn=ecs_resource.arn, tags=new_stop_tags
                )
        except Exception as ex:
            self._logger.warning(WARN_TAGGING_STOPPED, ecs_resource.id, str(ex))

    def _tag_started_resource(self, client, ecs_resource):
        start_tags = self._validate_ecs_tag_values(self._config.started_tags)
        if start_tags is None:
            start_tags = []
        start_tags_key_names = [t["Key"] for t in start_tags]
        stop_tags_keys = [
            t["Key"]
            for t in self._config.stopped_tags
            if t["Key"] not in start_tags_key_names
        ]
        try:
            if stop_tags_keys is not None and len(stop_tags_keys):
                self._logger.info(
                    INF_REMOVE_KEYS,
                    "stop",
                    ",".join(['"{}"'.format(k) for k in stop_tags_keys]),
                    ecs_resource.arn,
                )
                client.untag_resource(
                    resourceArn=ecs_resource.arn, tags=stop_tags_keys
                )
            if start_tags is not None and len(start_tags) > 0:
                self._logger.info(
                    INF_ADD_TAGS, "start", str(start_tags), ecs_resource.arn
                )
                new_start_tags = []
                for t in start_tags:
                    new_start_tags.append({"key":t["Key"], "value":t["Value"]})
                client.tag_resource(
                    resourceArn=ecs_resource.arn, tags=new_start_tags
                )
        except Exception as ex:
            self._logger.warning(WARN_TAGGING_STARTED, ecs_resource.id, str(ex))
