import datetime
import pprint
import sys

from safecast_deploy import aws_state, verbose_sleep
from safecast_deploy.aws_state import AwsTierType
from safecast_deploy.exceptions import EnvNotHealthyException, EnvUpdateTimedOutException


class SameEnv:
    def __init__(self, target_env_type, old_aws_state, new_aws_state, eb_client, result_logger):
        self.old_aws_env_state = old_aws_state.envs[target_env_type]
        self.new_aws_env_state = new_aws_state.envs[target_env_type]
        self._c = eb_client
        self._result_logger = result_logger

    def run(self):
        self.start_time = datetime.datetime.now(datetime.timezone.utc)

        self._check_environments()

        # Handle the worker tier first, to ensure that database
        # migrations are applied
        self._update_environment(AwsTierType.WORKER)
        self._update_environment(AwsTierType.WEB)

        result = self._generate_result()
        result_logger.log_result(result)

    # Make sure we're not trying to deploy on top of an environment in distress
    def _check_environments(self):
        for tier_type, tier in self.old_aws_env_state.envs:
            health = self._c.describe_environment_health(
                EnvironmentName=tier.name,
                AttributeNames=['HealthStatus', ]
            )['HealthStatus']
            if health != 'Ok':
                raise EnvNotHealthyException(
                    f"Environment {tier.name} has a health status of {health} and cannot be deployed to.",
                    tier.name,
                    health
                )

    def _generate_result(self):
        completed_time = datetime.datetime.now(datetime.timezone.utc)
        result = {
            'app': self.new_aws_env_state.aws_app_name,
            'completed_at': completed_time,
            'elapsed_time': (completed_time - self.start_time).total_seconds(),
            'env': self.new_aws_env_state.env_type.value,
            'event': 'same_env',
            'started_at': self.start_time,
        }
        for new_tier in self.new_aws_env_state.tiers:
            old_tier = self.old_aws_env_state.tiers[new_tier.tier]
            result.update(
                {
                    f'{new_tier.tier.value}': {
                        'env': new_tier.name,
                        'new_version': new_tier.version,
                        'new_version_parsed': new_tier.parsed_version.to_dict(),
                        'old_version': old_tier.version,
                        'old_version_parsed': old_tier.parsed_version.to_dict(),
                    },
                }
            )
            self._add_git(tier, result)

        return result

    def _add_git(self, role, result):
        repo_names = {
            'api': 'safecastapi',
            'ingest': 'ingest',
            'reporting': 'reporting',
        }
        if (old_tier.parsed_version.git_commit is not None) \
           and (new_tier.parsed_version.git_commit is not None):
            result[new_tier.tier.value]['github_diff'] = 'https://github.com/Safecast/{}/compare/{}...{}'.format(
                repo_names[self.old_aws_env_state.aws_app_name],
                old_tier.parsed_version.git_commit,
                new_tier.parsed_version.git_commit
            )

    def _print_result(self, result):
        print(json.dumps(result, sort_keys=True, indent=2))
        print("Deployment completed.", file=sys.stderr)

    def _update_environment(self, tier):
        if tier not in new_aws_state.tiers:
            return

        print(f"Deploying to the {tier.value} tier.", file=sys.stderr)
        env_name = self.new_aws_env_state.tiers[tier].name
        self._c.update_environment(
            ApplicationName=self.new_aws_env_state.aws_app_name,
            EnvironmentName=env_name,
            VersionLabel=self.new_aws_env_state.tiers[tier].version
        )

        self._wait_for_green(env_name)

    def _wait_for_green(self, env_name):
        print(f"Waiting for {env_name} health to return to normal.", file=sys.stderr)
        verbose_sleep(70)
        wait_seconds = 0
        while wait_seconds < 480:
            health = self._c.describe_environment_health(
                EnvironmentName=env_name,
                AttributeNames=['HealthStatus', ]
            )['HealthStatus']
            if health == 'Ok':
                print(f"{env_name} health has returned to normal.", file=sys.stderr)
                return
            verbose_sleep(40)
            wait_seconds += 40
        raise EnvUpdateTimedOutException(
            "f{env_name} health did not return to normal within 480 seconds.",
            env_name, 480
        )
