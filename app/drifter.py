from logzero import logger
import logging
import logzero
from urllib.parse import urlparse
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from concurrent.futures.thread import ThreadPoolExecutor
import re
import json
import time
import requests
import signal
import os
import boto3
import uuid
import zipfile
import subprocess
import settings

requested_to_quit = False
last_summary_emitted = 0

endpoint_definitions = None
alert_definitions = None
metrics_definitions = None
db = None


def main():
    logger.info("starting...")

    setup_signal_handling()

    global db
    db = settings.get_database()

    # get terraform version from state found with s3 bucket/key
    terraform_version = get_terraform_version(settings.TERRAFORM_S3_BUCKET, settings.TERRAFORM_S3_KEY)

    # install appropriate terraform version
    terraform_bin = install_terraform(terraform_version)

    # get current head of terraform repository
    # fetch that version as an archive and unzip it
    repo_folder = fetch_current_repo_head()

    # terraform init (with parameters)
    terraform_initialise(terraform_bin, repo_folder)

    # terraform plan (with parameters)
    result = terraform_plan(terraform_bin, repo_folder)

    # ship metrics


def signal_handler(signum, frame):
    logger.info(f"Caught signal {signum}")
    global requested_to_quit
    requested_to_quit = True


def setup_signal_handling():
    logger.info("setting up signal handling")
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


def get_file_or_s3(uri):
    logger.info(f"getting file URI {uri}")

    if uri.lower().startswith("s3://"):
        s3 = boto3.resource("s3")
        parse_result = urlparse(uri)
        s3_object = s3.Object(parse_result.netloc, parse_result.path.lstrip("/"))
        return s3_object.get()["Body"].read().decode("utf-8")

    return open(uri).read()


def download_file(url, filename, headers={}):
    with requests.get(url, stream=True, headers=headers) as r:
        r.raise_for_status()
        with open(filename, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)


def get_terraform_version(bucket, key):
    logger.info(f"getting Terraform version from remote state at s3://{bucket}/{key}")

    remote_state = json.loads(get_file_or_s3(f"s3://{bucket}/{key}"))

    version = remote_state["terraform_version"]
    logger.debug(f"terraform version = {version}")

    return version


def install_terraform(version):
    logger.info(f"installing Terraform version {version}")

    filename = f"terraform_{version}_linux_amd64"
    url = f"https://releases.hashicorp.com/terraform/{version}/{filename}.zip"
    zip = f"{settings.TMP_FOLDER}/{filename}.zip"
    out_path = f"{settings.TMP_FOLDER}/{filename}"
    bin = f"{out_path}/terraform"

    logger.debug(f"downloading Terraform from {url}")
    download_file(url=url, filename=zip)

    logger.debug(f"making output directory {out_path}")
    os.mkdir(out_path)

    logger.debug(f"unzipping archive {zip} to {out_path}")
    zip_output = subprocess.Popen(
        f"unzip -o {zip} -d {out_path}",
        cwd=settings.TMP_FOLDER,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    ).stderr.read()

    logger.debug(f"zip stderr output was: {zip_output}")

    return bin


def fetch_current_repo_head():
    logger.info(f"getting current HEAD of {settings.TERRAFORM_GITHUB_REPO}")

    api_url = f"https://api.github.com/repos/{settings.TERRAFORM_GITHUB_REPO}/branches/{settings.TERRAFORM_GITHUB_BRANCH}"

    r=requests.get(api_url, headers={
        "Authorization": f"token {settings.TERRAFORM_GITHUB_TOKEN}",
        "User-Agent": f"Drifter (Terraform monitor)"
    })

    parsed_json = json.loads(r.text)

    repo_sha = parsed_json["commit"]["sha"]

    logger.info(f"commit SHA was {repo_sha}")

    zip = f"{settings.TMP_FOLDER}/repo.zip"
    out_path = f"{settings.TMP_FOLDER}/repo"

    modified_repo_name = settings.TERRAFORM_GITHUB_REPO.replace("/", "-")
    full_repo_path = f"{out_path}/{modified_repo_name}-{repo_sha}"

    if os.path.isdir(full_repo_path):
        logger.info(f"skipping download as it already exists in {settings.TMP_FOLDER}")
    else:
        api_url = f"https://api.github.com/repos/{settings.TERRAFORM_GITHUB_REPO}/zipball/{repo_sha}"

        logger.debug(f"downloading repo from {api_url}")

        download_file(url=api_url, filename=zip, headers={
            "Authorization": f"token {settings.TERRAFORM_GITHUB_TOKEN}",
            "User-Agent": f"Drifter (Terraform monitor)"
        })

        logger.debug(f"making output directory {out_path}")
        os.mkdir(out_path)

        logger.debug(f"unzipping archive {zip} to {out_path}")
        zip_output = subprocess.Popen(
            f"unzip -o {zip} -d {out_path}",
            cwd=settings.TMP_FOLDER,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        ).stderr.read()

        logger.debug(f"zip stderr output was: {zip_output}")

    return full_repo_path


def exec_shell(command):
    subprocess.run(command)


def terraform_initialise(terraform_bin, repo_folder):
    logger.info(f"initialising Terraform ({terraform_bin})")

    candidate_folder = repo_folder
    if settings.TERRAFORM_GITHUB_FOLDER:
        candidate_folder = f"{candidate_folder}/{settings.TERRAFORM_GITHUB_FOLDER}"
        logger.info(f"using candidate repo folder {candidate_folder}")

    init_output = subprocess.Popen(
        f"{terraform_bin} init -input=false -lock=false -no-color",
        cwd=candidate_folder,
        shell=True,
        stdout=subprocess.PIPE
    ).stdout.read()

    logger.debug(f"terraform init output was: {init_output}")


def terraform_plan(terraform_bin, repo_folder):
    logger.info(f"planning Terraform ({terraform_bin}) using {repo_folder}")

    candidate_folder = repo_folder
    if settings.TERRAFORM_GITHUB_FOLDER:
        candidate_folder = f"{candidate_folder}/{settings.TERRAFORM_GITHUB_FOLDER}"
        logger.info(f"using candidate repo folder {candidate_folder}")

    plan_start_time = time.time()

    child = subprocess.Popen(
        f"{terraform_bin} plan --detailed-exitcode -input=false -lock=false -no-color",
        cwd=candidate_folder,
        shell=True,
        stdout=subprocess.PIPE
    )

    exit_code = child.returncode

    plan_time_taken = time.time() - plan_start_time

    plan_output = child.stdout.read()

    if exit_code == 1:
        # plan failed
        plan_error = child.stderr.read()
        logger.info(f"terraform plan failed. output was: {plan_output}")
        logger.info(f"error was: {plan_error}")
        return False
    else:
        # plan finished
        logger.debug(f"terraform plan output was: {plan_output}")

        logger.info(f"plan finished")

        resource_count = 0
        pending_add = 0
        pending_change = 0
        pending_destroy = 0
        pending_total = 0

        resource_regex = re.compile(r" Refreshing state\.\.\.")
        plan_regex = re.compile(r"^Plan (\d+) to add, (\d+) to change, (\d+) to destroy.")

        for plan_line in plan_output:
            # count number of resources
            if re.match(resource_regex, plan_line):
                resource_count = resource_count + 1

            m = re.match(plan_regex, plan_line)
            if m:
                pending_add = int(m.group(0))
                pending_change = int(m.group(1))
                pending_destroy = int(m.group(2))

        logger.debug(f"pending add: {pending_add}")
        logger.debug(f"pending change: {pending_change}")
        logger.debug(f"pending destroy: {pending_destroy}")

        pending_total = pending_add + pending_change + pending_destroy

        logger.debug(f"pending total: {pending_total}")

    return {
        "terraform_status": exit_code,
        "resource_count": resource_count,
        "pending_add": pending_add,
        "pending_change": pending_change,
        "pending_destroy": pending_destroy,
        "plan_time": plan_time_taken
    }


if __name__ == "__main__":
    if settings.DEBUG:
        logzero.loglevel(logging.DEBUG)
    else:
        logzero.loglevel(logging.INFO)

    main()
