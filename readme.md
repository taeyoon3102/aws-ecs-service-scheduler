# Extended AWS instance scheduler for ecs service
## 개요
aws 에서 제공하는 스케쥴러는 ec2, rds 에 대해서만 스케쥴링을 제공한다(2023/11/23 기준).  
ecs service 의 경우 켜져 있는 동안 비용이 나오기 때문에 dev 환경의 경우 스케쥴링을 통해 비용절감을 할 수 있다.  
해당 코드는 aws instance scheduler 1.5.0 version 에 기반하였다.  

## 특이사항
ecs service 는 desiredCount 값을 가지고 있기 때문에 켜고 끌 때 이 값을 별도로 처리해줘야 한다.  
이를 ecs service tag 에 저장하고 불러와 사용하도록 작성하였다.  

## 업데이트 예정
- desiredCount resize 기능
- aws cli pagination 관련 코드 보완

## 적용
### 1. lambda 에 권한 추가
aws cloudformation stack template 를 통해 생성되는 lambda function 에 아래와 같은 권한을 추가해줘야 한다  
```json
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Action": [
				"ecs:update*",
				"ecs:list*",
				"ecs:describe*",
				"ecs:TagResource",
				"ecs:UntagResource"
			],
			"Resource": "*",
			"Effect": "Allow",
			"Sid": "ECSCluster"
		}
	]
}
```

### 2. lambda 코드의 configuration 폴더 작업
path : instance_scheduler > configuration > config_admin.py

```python
...

class ConfigAdmin:
    """
    Implements admin api for Scheduler
    """

    TYPE_ATTR = "type"
    # regex for checking time formats H:MM and HH:MM
    TIME_REGEX = "^([0|1]?[0-9]|2[0-3]):[0-5][0-9]$"

    SUPPORTED_SERVICES = ["ec2", "rds", "ecs"] # here

...

```


### 3. lambda 코드의 schedulers 폴더 작업
#### 3.1 \_\_init\_\_.py
path : instance_scheduler > scheulders > \_\_init\_\_.py

```python
...

from instance_scheduler.schedulers.ec2_service import Ec2Service
from instance_scheduler.schedulers.rds_service import RdsService
from instance_scheduler.schedulers.ecs_service import EcsService # here

...

SCHEDULER_TYPES = {"ec2": Ec2Service, "rds": RdsService, "ecs": EcsService} #here

```

#### 3.2 ecs_service.py 파일 추가 및 코드 복사
path : instance_scheduler > scheulders > ecs_service.py

