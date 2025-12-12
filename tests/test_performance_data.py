import json
import tempfile
import os
import pytest
from unittest.mock import Mock
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatusOk
from girder_sivacor.worker_plugin.lib import DockerStatsCollectorThread, NpEncoder
from .conftest import (
    upload_test_file,
    submit_sivacor_job,
    get_submission_folder,
)


class TestDockerStatsCollectorThread:
    """Test the DockerStatsCollectorThread functionality."""

    def test_csv_header_creation(self):
        """Test that CSV file is created with proper headers."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "dockerstats")
            mock_container = Mock()

            # Mock container stats that will cause container_finished to return True immediately
            mock_container.stats.return_value = {"read": "0001-01-01T00:00:00Z"}
            mock_container.attrs = {"State": {"Status": "exited"}}

            stats_thread = DockerStatsCollectorThread(mock_container, output_path)

            # Run the thread briefly
            stats_thread.daemon = True
            stats_thread.start()
            stats_thread.join(timeout=1)

            # Check that CSV file was created with header
            csv_file = output_path + ".csv"
            assert os.path.exists(csv_file), "CSV file should be created"

            with open(csv_file, "r") as f:
                content = f.read()
                expected_header = (
                    "Timestamp,CPU %,Memory Usage,Memory Limit,Network RX,Network TX,"
                    "Block IO Read,Block IO Write,PIDs\n"
                )
                assert content.startswith(
                    expected_header
                ), "CSV should have correct header"

    def test_convert_parameter_functionality(self):
        """Test the convert parameter in calculation methods."""
        stats_thread = DockerStatsCollectorThread(Mock(), "/tmp/test")

        # Test memory calculation with convert=False
        mock_data = {
            "memory_stats": {
                "usage": 1024000000,  # ~1GB
                "limit": 2048000000,  # ~2GB
            }
        }

        # With convert=False, should return raw bytes
        usage_raw, limit_raw = stats_thread.calculate_memory(mock_data, convert=False)
        assert usage_raw == 1024000000
        assert limit_raw == 2048000000

        # With convert=True (default), should return formatted strings
        usage_formatted, limit_formatted = stats_thread.calculate_memory(
            mock_data, convert=True
        )
        assert isinstance(usage_formatted, str)
        assert isinstance(limit_formatted, str)
        assert "GiB" in usage_formatted or "MiB" in usage_formatted

    def test_blkio_convert_parameter(self):
        """Test block I/O calculation with convert parameter."""
        stats_thread = DockerStatsCollectorThread(Mock(), "/tmp/test")

        mock_data = {
            "blkio_stats": {
                "io_service_bytes_recursive": [
                    {"op": "Read", "value": 1024000},
                    {"op": "Write", "value": 2048000},
                ]
            }
        }

        # With convert=False, should return raw bytes
        read_raw, write_raw = stats_thread.calculate_blkio_bytes(
            mock_data, convert=False
        )
        assert read_raw == 1024000
        assert write_raw == 2048000

        # With convert=True, should return formatted strings
        read_formatted, write_formatted = stats_thread.calculate_blkio_bytes(
            mock_data, convert=True
        )
        assert isinstance(read_formatted, str)
        assert isinstance(write_formatted, str)

    def test_network_convert_parameter(self):
        """Test network calculation with convert parameter."""
        stats_thread = DockerStatsCollectorThread(Mock(), "/tmp/test")

        mock_data = {
            "networks": {
                "eth0": {
                    "rx_bytes": 1024000,
                    "tx_bytes": 2048000,
                }
            }
        }

        # With convert=False, should return raw bytes
        rx_raw, tx_raw = stats_thread.calculate_network_bytes(mock_data, convert=False)
        assert rx_raw == 1024000
        assert tx_raw == 2048000

        # With convert=True, should return formatted strings
        rx_formatted, tx_formatted = stats_thread.calculate_network_bytes(
            mock_data, convert=True
        )
        assert isinstance(rx_formatted, str)
        assert isinstance(tx_formatted, str)


class TestNpEncoder:
    """Test the NumPy JSON encoder."""

    def test_numpy_integer_encoding(self):
        """Test encoding of NumPy integers."""
        import numpy as np

        data = {
            "regular_int": 42,
            "numpy_int": np.int64(42),
            "numpy_int32": np.int32(42),
        }

        encoded = json.dumps(data, cls=NpEncoder)
        decoded = json.loads(encoded)

        assert decoded["regular_int"] == 42
        assert decoded["numpy_int"] == 42
        assert decoded["numpy_int32"] == 42

        # All should be regular Python ints in the decoded result
        assert isinstance(decoded["numpy_int"], int)
        assert isinstance(decoded["numpy_int32"], int)

    def test_numpy_float_encoding(self):
        """Test encoding of NumPy floats."""
        import numpy as np

        data = {
            "regular_float": 3.14,
            "numpy_float": np.float64(3.14),
            "numpy_float32": np.float32(3.14),
        }

        encoded = json.dumps(data, cls=NpEncoder)
        decoded = json.loads(encoded)

        assert abs(decoded["regular_float"] - 3.14) < 1e-10
        assert abs(decoded["numpy_float"] - 3.14) < 1e-10
        assert abs(decoded["numpy_float32"] - 3.14) < 1e-5  # float32 has less precision

        # All should be regular Python floats in the decoded result
        assert isinstance(decoded["numpy_float"], float)
        assert isinstance(decoded["numpy_float32"], float)

    def test_numpy_array_encoding(self):
        """Test encoding of NumPy arrays."""
        import numpy as np

        data = {
            "regular_list": [1, 2, 3],
            "numpy_array": np.array([1, 2, 3]),
            "nested_array": np.array([[1, 2], [3, 4]]),
        }

        encoded = json.dumps(data, cls=NpEncoder)
        decoded = json.loads(encoded)

        assert decoded["regular_list"] == [1, 2, 3]
        assert decoded["numpy_array"] == [1, 2, 3]
        assert decoded["nested_array"] == [[1, 2], [3, 4]]


@pytest.mark.plugin("sivacor")
def test_dockerstats_file_creation(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test that dockerstats files are created and uploaded for each stage."""
    stages = [
        {
            "image_name": "rocker/r-ver",
            "image_tag": "4.5.2",
            "main_file": "main.R",
        },
    ]

    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "pi-R.zip")

    # Submit SIVACOR job
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    job = resp.json

    # Verify job completion
    job = Job().load(job["_id"], force=True)
    assert job["status"] == JobStatus.SUCCESS

    # Get submission folder
    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    assertStatusOk(resp)
    submission_folder = resp.json[0]

    # Verify dockerstats file was created and uploaded
    folder_obj = Folder().load(submission_folder["_id"], force=True)

    # Find dockerstats file
    dockerstats_items = list(
        Folder().childItems(
            folder_obj,
            filters={"meta.type": "dockerstats"},
            limit=1,
        )
    )

    assert len(dockerstats_items) == 1, "Dockerstats file should be created"

    # Verify the dockerstats file contains performance data
    with Item().childFiles(dockerstats_items[0]) as files:
        for fobj in files:
            with File().open(fobj) as f:
                content = f.read().decode("utf-8")

                # Should contain timestamps and performance metrics
                assert " - " in content, "Should contain formatted performance lines"
                assert "%" in content, "Should contain CPU percentage"
                assert "/" in content, "Should contain memory and network usage"


@pytest.mark.plugin("sivacor")
def test_performance_data_integration(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test complete performance data integration including CSV processing."""
    stages = [
        {
            "image_name": "rocker/r-ver",
            "image_tag": "4.5.2",
            "main_file": "main.R",
        },
    ]

    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "pi-R.zip")

    # Submit SIVACOR job
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    job = resp.json

    # Verify job completion
    job = Job().load(job["_id"], force=True)
    assert job["status"] == JobStatus.SUCCESS

    # Get submission folder
    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    assertStatusOk(resp)
    submission_folder = resp.json[0]

    # Verify performance data JSON file was created
    folder_obj = Folder().load(submission_folder["_id"], force=True)
    performance_items = list(
        Folder().childItems(
            folder_obj,
            filters={"name": "performance_data_stage_1.json"},
            limit=1,
        )
    )

    assert len(performance_items) == 1, "Performance data JSON should be created"

    # Verify performance data content and structure
    with Item().childFiles(performance_items[0]) as files:
        for fobj in files:
            with File().open(fobj) as f:
                performance_data = json.load(f)

                # Verify system information
                required_system_fields = [
                    "Architecture",
                    "KernelVersion",
                    "OperatingSystem",
                    "OSType",
                    "MemTotal",
                    "NCPU",
                ]
                for field in required_system_fields:
                    assert (
                        field in performance_data
                    ), f"Performance data should contain {field}"

                # Verify container information
                required_container_fields = ["ImageRepoTags", "StartedAt", "FinishedAt"]
                for field in required_container_fields:
                    assert (
                        field in performance_data
                    ), f"Performance data should contain {field}"

                # Verify that timestamps are valid
                assert performance_data["StartedAt"] != ""
                assert performance_data["FinishedAt"] != ""

                # If CSV was processed, verify performance metrics
                if "MaxCPUPercent" in performance_data:
                    assert isinstance(performance_data["MaxCPUPercent"], (int, float))
                    # assert performance_data["MaxCPUPercent"] >= 0

                if "MaxMemoryUsage" in performance_data:
                    assert isinstance(performance_data["MaxMemoryUsage"], (int, float))
                    # assert performance_data["MaxMemoryUsage"] >= 0
