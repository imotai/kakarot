import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import pyperclip
import rlp
from dotenv import load_dotenv
from eth.vm.forks.cancun.blocks import CancunBlock
from web3 import Web3

from kakarot_scripts.constants import BEACON_ROOT_ADDRESS
from kakarot_scripts.ef_tests.fetch import EF_TESTS_PARSED_DIR

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

load_dotenv(override=True)

TESTS_PATH = Path("tests/ef_tests/test_data/BlockchainTests/GeneralStateTests")
TEST_NAME = os.getenv("TEST_NAME")
if TEST_NAME is None:
    raise ValueError("Please set TEST_NAME")
TEST_PARENT_FOLDER = os.getenv("TEST_PARENT_FOLDER", "")
RPC_ENDPOINT = "http://127.0.0.1:8545"


class AnvilHandler:
    def __init__(self, data):
        try:
            block = get_block(data)
            ts = block.header.timestamp
            anvil = subprocess.Popen(
                f"anvil --timestamp {ts} --hardfork cancun",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=True,
            )
            # Wait for anvil to start
            time.sleep(1)
        except Exception as e:
            raise Exception("Could not launch anvil") from e
        self.anvil = anvil

    def wait(self):
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)
        logger.info("Anvil still running... (press Ctrl+C to stop)")
        while True:
            time.sleep(1)

    def signal_handler(self, signum, _):
        logger.warning(f"Signal {signum} received, shutting down anvil...")
        self.anvil.terminate()
        exit(0)


def get_test_file():
    tests = [
        file_name
        for file_name in os.listdir(EF_TESTS_PARSED_DIR)
        if TEST_NAME in file_name and TEST_PARENT_FOLDER in file_name
    ]

    if len(tests) == 0:
        if len(TEST_NAME) > 255:
            raise ValueError(
                f"Test name '{TEST_NAME}' could not be parsed because it exceeds the maximum length of 255 characters."
            )
        raise ValueError(
            f"Test '{TEST_NAME}' not found. Please ensure that you are using the valid EF-Test name, not the sanitized identifier used in the runner. If the test name is longer than 255 characters, it might be invalid."
        )
    if len(tests) > 1:
        if TEST_PARENT_FOLDER == "":
            raise ValueError(
                f"Test {TEST_NAME} is ambiguous, please set TEST_PARENT_FOLDER to test file folder"
            )

        raise ValueError(f"Test {TEST_NAME} not found")

    return json.loads((EF_TESTS_PARSED_DIR / tests[0]).read_text())


def connect_anvil():
    w3 = Web3(Web3.HTTPProvider(RPC_ENDPOINT, request_kwargs={"timeout": 60}))
    if not w3.is_connected():
        raise Exception("Not connected to anvil endpoint")
    return w3


def set_pre_state(w3, data):
    for address, account in data["pre"].items():
        w3.provider.make_request("anvil_setCode", [address, account["code"]])
        w3.provider.make_request("anvil_setBalance", [address, account["balance"]])
        w3.provider.make_request("anvil_setNonce", [address, account["nonce"]])
        for k, v in account["storage"].items():
            w3.provider.make_request(
                "anvil_setStorageAt",
                [address, f"0x{int(k, 16):064x}", f"0x{int(v, 16):064x}"],
            )


def get_block(data):
    try:
        block_rlp = data["blocks"][0]["rlp"]
        block = rlp.decode(bytes.fromhex(block_rlp[2:]), CancunBlock)
    except Exception as e:
        raise Exception("Could not find block in test data") from e
    return block


def set_block(w3, data):
    block = get_block(data)
    if len(block.transactions) > 0 and block.transactions[0].chain_id is not None:
        w3.provider.make_request("anvil_setChainId", [block.transactions[0].chain_id])
    w3.provider.make_request("anvil_setCoinbase", [block.header.coinbase.hex()])
    w3.provider.make_request(
        "anvil_setNextBlockBaseFeePerGas", [block.header.base_fee_per_gas]
    )
    w3.provider.make_request("evm_setBlockGasLimit", [block.header.gas_limit])


def send_transaction(w3, transaction):
    tx_hash = w3.eth.send_raw_transaction(transaction.encode()).hex()
    return tx_hash


def check_post_state(w3, data):
    for address, account in data["postState"].items():
        address = Web3.to_checksum_address(address)
        if address == Web3.to_checksum_address(
            BEACON_ROOT_ADDRESS
        ):  # Skip beacon root address validation
            continue
        try:
            assert w3.eth.get_balance(address) == int(
                account["balance"], 16
            ), f'balance error: {w3.eth.get_balance(address)} != {int(account["balance"], 16)}'
            assert w3.eth.get_transaction_count(address) == int(
                account["nonce"], 16
            ), f"nonce error: {w3.eth.get_transaction_count(address)} != {int(account['nonce'], 16)}"
            assert w3.eth.get_code(address) == bytes.fromhex(
                account["code"][2:]
            ), f'code error: {w3.eth.get_code(address)} != {bytes.fromhex(account["code"][2:])}'
            for k, v in account["storage"].items():
                assert int.from_bytes(w3.eth.get_storage_at(address, k), "big") == int(
                    v, 16
                ), f'storage error at key {k}: {int.from_bytes(w3.eth.get_storage_at(address, k), "big")} != {int(v, 16)}'
        except Exception as e:
            raise ValueError(f"Post state does not match for {address}, got {e}") from e
    logger.info("Post state is valid")


def main():
    test = get_test_file()
    # Launch anvil
    handler = AnvilHandler(test)
    try:
        provider = connect_anvil()
        # Set test state
        set_pre_state(provider, test)
        set_block(provider, test)

        # Send transactions
        block = get_block(test)
        tx_hashes = []
        if len(block.transactions) == 0:
            raise ValueError("Could not find transaction in test data")
        for tx in block["transactions"]:
            tx_hash = send_transaction(provider, tx)
            provider.eth.wait_for_transaction_receipt(tx_hash)
            tx_hashes.append(tx_hash)

        # Check post state
        check_post_state(provider, test)

        logger.info("Running transactions:")
        for i, tx_hash in enumerate(tx_hashes, start=1):
            command = f"cast run {tx_hash}"
            _ = subprocess.run(command, shell=True)
            debug_command = f"cast run {tx_hash} --debug"
            logger.info(f"Run `{debug_command}` to debug transaction {i} \n")

        first_command = f"cast run {tx_hashes[0]} --debug"
        pyperclip.copy(first_command)
        logger.info(f"First command `{first_command}` copied to clipboard")

    except Exception as e:
        handler.anvil.terminate()
        raise e

    # Wait for sig term to stop anvil
    handler.wait()


if __name__ == "__main__":
    main()
