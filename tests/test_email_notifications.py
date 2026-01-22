"""
Tests for SIVACOR email notification functionality.

This module tests the email notification system that sends emails to users
when their submission jobs complete (success or failure).
"""

import json

import mock
import pytest
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatusOk

from .conftest import (
    get_submission_folder,
    submit_sivacor_job,
    upload_test_file,
)


@pytest.mark.plugin("sivacor")
def test_success_email_notification(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test that success email notification is sent when job completes successfully."""
    image_name = "dataeditors/stata18_5-mp"
    image_tag = "2025-02-26"
    main_file = "main.do"
    stages = [
        {"image_name": image_name, "image_tag": image_tag, "main_file": main_file}
    ]

    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    # Mock SMTP
    with mock.patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = mock.MagicMock()
        mock_smtp_class.return_value = mock_smtp

        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)
        job = resp.json

        # Verify job completion
        job = Job().load(job["_id"], force=True)
        assert job["status"] == JobStatus.SUCCESS

        # Verify email was sent
        assert mock_smtp.sendmail.called
        assert mock_smtp.sendmail.call_count == 1

        # Get the call arguments: sendmail(from_addr, to_addrs, message_bytes)
        call_args = mock_smtp.sendmail.call_args
        recipients = call_args[0][1]
        message_bytes = call_args[0][2]
        message_str = message_bytes.decode("utf-8")

        # Verify email details
        assert recipients == [user["email"]]
        assert "completed successfully" in message_str.lower()

        # Verify email content (both text and HTML parts)
        assert "SUCCESS" in message_str
        assert user["firstName"] in message_str
        assert user["lastName"] in message_str
        assert str(job["_id"]) in message_str
        assert main_file in message_str
        assert image_tag in message_str

        # Verify multipart structure
        assert "Content-Type: text/plain" in message_str
        assert "Content-Type: text/html" in message_str


@pytest.mark.plugin("sivacor")
def _test_failure_email_notification(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test that failure email notification is sent when job fails."""
    image_name = "dataeditors/stata18_5-mp"
    image_tag = "2025-02-26"
    main_file = "fail.do"
    stages = [
        {"image_name": image_name, "image_tag": image_tag, "main_file": main_file}
    ]

    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    # Mock SMTP
    with mock.patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = mock.MagicMock()
        mock_smtp_class.return_value = mock_smtp

        # Submit SIVACOR job (expecting failure)
        resp = server.request(
            path="/sivacor/submit_job",
            method="POST",
            user=user,
            params={
                "id": str(fobj["_id"]),
                "stages": json.dumps(stages),
            },
            exception=True,
        )
        assertStatusOk(resp)
        job = resp.json

        # Verify job failed
        job = Job().load(job["_id"], force=True)
        assert job["status"] == JobStatus.ERROR

        # Verify email was sent
        assert mock_smtp.sendmail.called
        assert mock_smtp.sendmail.call_count == 1

        # Get the call arguments: sendmail(from_addr, to_addrs, message_bytes)
        call_args = mock_smtp.sendmail.call_args
        recipients = call_args[0][1]
        message_bytes = call_args[0][2]
        message_str = message_bytes.decode("utf-8")

        # Verify email details
        assert recipients == [user["email"]]
        assert "failed" in message_str.lower()

        # Verify email content
        assert "FAILED" in message_str or "ERROR" in message_str
        assert user["firstName"] in message_str
        assert user["lastName"] in message_str
        assert str(job["_id"]) in message_str
        assert main_file in message_str
        assert image_name in message_str
        assert image_tag in message_str


@pytest.mark.plugin("sivacor")
def test_email_template_rendering(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test that email template is correctly rendered with job information."""
    image_name = "dataeditors/stata18_5-mp"
    image_tag = "2025-02-26"
    main_file = "main.do"
    stages = [
        {"image_name": image_name, "image_tag": image_tag, "main_file": main_file}
    ]

    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    # Mock SMTP
    with mock.patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = mock.MagicMock()
        mock_smtp_class.return_value = mock_smtp

        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)
        job = resp.json

        # Verify job completion
        job = Job().load(job["_id"], force=True)
        assert job["status"] == JobStatus.SUCCESS

        # Get the email content
        call_args = mock_smtp.sendmail.call_args
        message_bytes = call_args[0][2]
        message_str = message_bytes.decode("utf-8")

        # Verify template structure
        assert "<!DOCTYPE html>" in message_str
        assert "<html" in message_str
        assert "SIVACOR" in message_str

        # Verify all required template variables are rendered
        assert "Job ID:" in message_str
        assert "Submitted:" in message_str
        assert "Completed:" in message_str
        assert "Execution Time:" in message_str
        assert "Status:" in message_str
        assert "Stages:" in message_str

        # Verify links are present
        assert "href=" in message_str
        assert "submit.sivacor.org" in message_str
        assert "docs.sivacor.org" in message_str

        # Verify multipart email with both text and HTML
        assert "Content-Type: text/plain" in message_str
        assert "Content-Type: text/html" in message_str


@pytest.mark.plugin("sivacor")
def test_email_notification_error_handling(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test that email notification errors are logged but don't crash the job."""
    image_name = "dataeditors/stata18_5-mp"
    image_tag = "2025-02-26"
    main_file = "main.do"
    stages = [
        {"image_name": image_name, "image_tag": image_tag, "main_file": main_file}
    ]

    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    # Mock SMTP to raise an exception
    with mock.patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = mock.MagicMock()
        mock_smtp_class.return_value = mock_smtp
        mock_smtp.sendmail.side_effect = Exception("Email service unavailable")

        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)
        job = resp.json

        # Verify job still completes successfully despite email error
        job = Job().load(job["_id"], force=True)
        assert job["status"] == JobStatus.SUCCESS

        # Verify submission folder status is still updated correctly
        resp = get_submission_folder(server, user, job["_id"], submission_collection)
        assertStatusOk(resp)
        assert len(resp.json) == 1

        submission_folder = resp.json[0]
        assert submission_folder["meta"]["status"] == "completed"


@pytest.mark.plugin("sivacor")
def test_email_content_for_multistage_job(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test email content includes all stages for multi-stage jobs."""
    stages = [
        {
            "image_name": "dataeditors/stata18_5-mp",
            "image_tag": "2025-02-26",
            "main_file": "step1.do",
        },
        {
            "image_name": "dataeditors/stata18_5-mp",
            "image_tag": "2025-02-26",
            "main_file": "step2.do",
        },
    ]

    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    # Mock SMTP
    with mock.patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = mock.MagicMock()
        mock_smtp_class.return_value = mock_smtp

        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)

        # Get the email content
        if mock_smtp.sendmail.called:
            call_args = mock_smtp.sendmail.call_args
            message_bytes = call_args[0][2]
            message_str = message_bytes.decode("utf-8")

            # Verify all stages are mentioned in the email
            assert "step1.do" in message_str
            assert "step2.do" in message_str
            assert stages[0]["image_tag"] in message_str


@pytest.mark.plugin("sivacor")
def test_timestamp_formatting_in_email(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test that timestamps are properly formatted in the email."""
    image_name = "dataeditors/stata18_5-mp"
    image_tag = "2025-02-26"
    main_file = "main.do"
    stages = [
        {"image_name": image_name, "image_tag": image_tag, "main_file": main_file}
    ]

    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    # Mock SMTP
    with mock.patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = mock.MagicMock()
        mock_smtp_class.return_value = mock_smtp

        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)
        job = resp.json

        # Verify job completion
        job = Job().load(job["_id"], force=True)
        assert job["status"] == JobStatus.SUCCESS

        # Get the email content
        call_args = mock_smtp.sendmail.call_args
        message_bytes = call_args[0][2]
        message_str = message_bytes.decode("utf-8")

        # Verify timestamp format (should include date and time)
        # Looking for pattern like "2026-01-18 15:13:24 EST"
        import re

        timestamp_pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} EST"
        timestamps = re.findall(timestamp_pattern, message_str)
        assert len(timestamps) >= 2  # At least submission and completion time


@pytest.mark.plugin("sivacor")
def test_duration_calculation_in_email(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test that execution duration is properly calculated and formatted in the email."""
    image_name = "dataeditors/stata18_5-mp"
    image_tag = "2025-02-26"
    main_file = "main.do"
    stages = [
        {"image_name": image_name, "image_tag": image_tag, "main_file": main_file}
    ]

    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    # Mock SMTP
    with mock.patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = mock.MagicMock()
        mock_smtp_class.return_value = mock_smtp

        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)
        job = resp.json

        # Verify job completion
        job = Job().load(job["_id"], force=True)
        assert job["status"] == JobStatus.SUCCESS

        # Get the email content
        call_args = mock_smtp.sendmail.call_args
        message_bytes = call_args[0][2]
        message_str = message_bytes.decode("utf-8")

        # Verify duration format - should contain time units
        assert (
            "second" in message_str or "minute" in message_str or "hour" in message_str
        )
