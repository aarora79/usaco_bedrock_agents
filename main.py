import os
import re
import time
import json
import weave
import random
import logging
from pathlib import Path
from litellm import completion
from litellm import RateLimitError

# Setup logging
logging.basicConfig(format='[%(asctime)s] p%(process)s {%(filename)s:%(lineno)d} %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Litellm needs region to be set
os.environ["AWS_REGION_NAME"] = "us-east-1"

# sometimes the model refused to generate content due to internal guardrails
# so this is a custom exception to catch that error
class NoContentGeneratedException(Exception):
    pass

# Canned code to use in case the model generates misformatted code
# so this code will also cause the test to fail so in that sense the overall
# accuracy of the model for this benchmark remain unaffected and this code simply
# helps the harness to move to the next problem in the benchmark
FAILED_RESPONSE = """
import sys

def main():
    input = sys.stdin.read
    data = input().split()
    
    # do nothing, this is a canned response so that the eval for
    # this task can silently fail

    print(data)

if __name__ == "__main__":
    main()
"""

# Regular expression pattern to extract Python code blocks from text
# Matches content between ```python and ``` markers, capturing everything in between
REGEX_FOR_PY_CODE_EXTRACTION: str = r"```python\n(.*?)```"

def _get_python_code(
    text: str, 
    regex_for_code_extraction: str = REGEX_FOR_PY_CODE_EXTRACTION,
    failure_response: str = FAILED_RESPONSE
) -> str:
    """
    Extracts Python code from text that contains markdown-style code blocks.
    
    Args:
        text (str): The input text containing Python code blocks
        regex_for_code_extraction (str): Regular expression pattern to match code blocks
            Defaults to REGEX_FOR_PY_CODE_EXTRACTION
        failure_response (str): Response to return if no code is found
            Defaults to FAILED_RESPONSE
    
    Returns:
        str: The extracted Python code if found, otherwise returns the failure_response
    
    Note:
        - Expects code to be formatted in markdown style with ```python and ``` markers
        - Uses re.DOTALL flag to match newlines within the code block
    """
    # Search for all matches of the regex pattern in the text
    # re.DOTALL allows the dot (.) to match newline characters
    matches = re.findall(regex_for_code_extraction, text, re.DOTALL)
    
    # If no matches found, log an error and return the failure response
    if matches is None:
        logger.error(
            f"no python code found in {text}, returning the canned failure response\n{failure_response}"
        )
        return failure_response
    
    # Return the first code block found (matches[0] contains the content between markers)
    return matches[0]

def _process_task(task_id: str, model_name: str, formatted_prompt: str, inference_params: dict) -> str:
    """
    Runs inference for a prompt using the specified model. Retry after sleep logic is in place
    in case of exceptions.
    
    Args:
        task_id (str): Identifier for the task being run
        model_name (str): The Amazon Bedrock model id to use, needs to start with "bedrock/"
        formatted_prompt (str): Prompt for inference
        inference_params (Dict): inference parameters such as max_tokens, tempertature and n
    Returns:
        str: The completion generated by the model
    
    Note:
        - Raises exception in case retries are exhausedted
    """
    max_retries: int = 10 # set to a rather higher value for Amazon Nova
    retry_delay: int = 60  # seconds
    print(f"formatted_prompt={formatted_prompt}")
    for attempt in range(max_retries):
        try:
            # run inference
            response = completion(
                    model=model_name,
                    model_id=None,
                    messages=[{"role": "user", "content": formatted_prompt}],
                    max_tokens=inference_params["max_tokens"],
                    temperature=inference_params["temperature"],
                    n=inference_params["n"],
                )
            # Debug: logger.info raw response
            logger.info(f"Raw Response: {response}")
            # check if we received an empty response, for example the model saying something like
            # "The generated text has been blocked by our content filters."
            if response['usage']['completion_tokens'] == 0:
                content = response["choices"][0]["message"]["content"]
                raise NoContentGeneratedException(f"completion tokens is 0, content={content}")
            return response

        except NoContentGeneratedException as e:
            if attempt < max_retries - 1:
                # increase delay with every retry and add some random jitter to the delay
                this_retry_delay = retry_delay * (attempt + 1) + random.randint(1, 10)
                logger.error(f"{e}, task {task_id} on attempt {attempt + 1}. Waiting {retry_delay} seconds...")
                time.sleep(this_retry_delay)
                continue
            else:
                logger.error(f"max retries exceeded for task {task_id}")
                raise  # Re-raise the exception if we've exhausted all retries    
        except RateLimitError as e:
            if attempt < max_retries - 1:
                # increase delay with every retry and add some random jitter to the delay
                this_retry_delay = retry_delay * (attempt + 1) + random.randint(1, 10)
                logger.error(f"{e}, task {task_id} on attempt {attempt + 1}. Waiting {this_retry_delay} seconds...")
                time.sleep(this_retry_delay)
                continue
            else:
                logger.error(f"max retries exceeded for task {task_id}")
                raise  # Re-raise the exception if we've exhausted all retries
                
        except Exception as e:
            logger.error(f"Unexpected error processing task {task_id}: {str(e)}")
            raise


def run(input: dict[str, dict], **kwargs) -> dict[str, str]:
    """
    Runs inference for each task in the benchmark.
    
    Args:
        input (dict): dictionary containing info for the tasks being run
        kwargs (dict): model name, prompt template path and other params needed for inference
    Returns:
        dict: Input dictionary with the "response" (completion) field added
    
    """
    assert "model_name" in kwargs, "model_name is required"
    logger.info(f"model Name={kwargs['model_name']}, prompt_template_path={kwargs.get('prompt_template_path')}")

    # result for each task
    results: dict = {}
    # generated code for each task
    code: dict = {}

    # Iterate through all the tasks and get responses from each task
    for task_id, task in input.items():
        # Debug: logger.info task details        
        logger.info(f"Processing Task ID: {task_id}, Task: {json.dumps(task, indent=2)}")
        prompt_template = ""
        template_path = kwargs["prompt_template_path"]
        if os.path.exists(template_path):
            with open(template_path, 'r') as f:
                prompt_template = f.read()
        else:
            raise FileNotFoundError(f"Prompt template not found at {template_path}, current file path is {os.path.abspath(__file__)}")
        
        # Debug: logger.info weave attributes before entering the context
        logger.info(f"Setting weave attributes for Task ID {task_id}")
        weave_attrs = {"weave_task_id": task_id}
        logger.info(f"Weave Attributes:{weave_attrs}")

        with weave.attributes(weave_attrs):
            try:
                # Debug: Confirm weave context is active
                logger.info("Weave Context Active: Attributes set")

                ########################################################################
                # inference parameters used, these are important as they can directly
                # impact the quality of the code generated
                ########################################################################
                inference_params = dict(max_tokens=2000, temperature=0.1, n=1)

                logger.info("Sending request to Bedrock with parameters: {inference_params}")
                # logger.info("Messages:", [{"role": "user", "content": input}])
                #logger.info(f"Passing the input to the bedrock model: {task['input']}")
                formatted_prompt = prompt_template.format(question=task['description'])
                logger.info(f"formatted prompt: {formatted_prompt}")

                # run inference              
                response = _process_task(task_id,
                                         kwargs["model_name"],
                                         formatted_prompt,
                                         inference_params)
                # Debug: logger.info raw response
                logger.debug(f"Raw Response: {response}")

                # Extract the content and store it in results
                results[task_id] = response["choices"][0]["message"]["content"]
                logger.info(f"response content to the code issue: {results[task_id]}")

                # extract the python code from the full output
                python_code = _get_python_code(results[task_id])
                code[task_id] = python_code
                logger.info(f"task_id={task_id}, python code=\n{python_code}")

                # Confirm successful processing for the task
                logger.info(f"Task ID {task_id} processed successfully")

            except Exception as e:
                # logger.info exception details
                logger.info(f"Error processing Task ID {task_id}: {e}")
                logger.error(f"going to use a canned response, this will fail the evaluation for this task but allow the rest of the tests to proceed")
                code[task_id] = FAILED_RESPONSE
    
    # assign the response field for all tasks
    for task_id, task in input.items():
        input[task_id]['response'] = code[task_id]

    # final results
    logger.debug("Final Results: {input}")

    return input

if __name__ == "__main__":
    
    # sample input
    input = json.loads(Path("input.txt").read_text())

    # model name and prompt template
    kwargs = dict(model_name="bedrock/amazon.nova-lite-v1:0",
                  prompt_template_path="prompt_templates/nova.txt")
    logger.info(f"kwargs={kwargs}")

    # run the agent code
    run(input, **kwargs)
    logger.info("all done")
    
