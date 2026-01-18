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

    # Mock email sending
    with mock.patch("girder.utility.mail_utils.sendMail") as mock_send_mail:
        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)
        job = resp.json

        # Verify job completion
        job = Job().load(job["_id"], force=True)
        assert job["status"] == JobStatus.SUCCESS

        # Verify email was sent
        assert mock_send_mail.called
        assert mock_send_mail.call_count == 1

        # Get the call arguments
        call_args = mock_send_mail.call_args
        subject = call_args[0][0]
        html_content = call_args[0][1]
        recipients = call_args[0][2]

        # Verify email details
        assert "completed successfully" in subject.lower()
        assert recipients == [user["email"]]

        # Verify email content
        assert "SUCCESS" in html_content
        assert user["firstName"] in html_content
        assert user["lastName"] in html_content
        assert str(job["_id"]) in html_content
        assert "completed successfully" in html_content.lower()
        assert main_file in html_content
        assert image_name in html_content
        assert image_tag in html_content


@pytest.mark.plugin("sivacor")
def test_failure_email_notification(
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

    # Mock email sending
    with mock.patch("girder.utility.mail_utils.sendMail") as mock_send_mail:
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
        assert mock_send_mail.called
        assert mock_send_mail.call_count == 1

        # Get the call arguments
        call_args = mock_send_mail.call_args
        subject = call_args[0][0]
        html_content = call_args[0][1]
        recipients = call_args[0][2]

        # Verify email details
        assert "failed" in subject.lower()
        assert recipients == [user["email"]]

        # Verify email content
        assert "FAILED" in html_content or "ERROR" in html_content
        assert user["firstName"] in html_content
        assert user["lastName"] in html_content
        assert str(job["_id"]) in html_content
        assert main_file in html_content
        assert image_name in html_content
        assert image_tag in html_content


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

    # Mock email sending
    with mock.patch("girder.utility.mail_utils.sendMail") as mock_send_mail:
        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)
        job = resp.json

        # Verify job completion
        job = Job().load(job["_id"], force=True)
        assert job["status"] == JobStatus.SUCCESS

        # Get the email content
        call_args = mock_send_mail.call_args
        html_content = call_args[0][1]

        # Verify template structure
        assert "<!DOCTYPE html>" in html_content
        assert "<html" in html_content
        assert "SIVACOR" in html_content

        # Verify all required template variables are rendered
        assert "Job ID:" in html_content
        assert "Submitted:" in html_content
        assert "Completed:" in html_content
        assert "Execution Time:" in html_content
        assert "Status:" in html_content
        assert "Stages:" in html_content

        # Verify links are present
        assert "href=" in html_content
        assert "submit.sivacor.org" in html_content
        assert "docs.sivacor.org" in html_content


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

    # Mock email sending to raise an exception
    with mock.patch(
        "girder.utility.mail_utils.sendMail",
        side_effect=Exception("Email service unavailable"),
    ):
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

    # Mock email sending
    with mock.patch("girder.utility.mail_utils.sendMail") as mock_send_mail:
        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)

        # Get the email content
        if mock_send_mail.called:
            call_args = mock_send_mail.call_args
            html_content = call_args[0][1]

            # Verify all stages are mentioned in the email
            assert "step1.do" in html_content
            assert "step2.do" in html_content
            assert stages[0]["image_name"] in html_content
            assert stages[0]["image_tag"] in html_content


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

    # Mock email sending
    with mock.patch("girder.utility.mail_utils.sendMail") as mock_send_mail:
        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)
        job = resp.json

        # Verify job completion
        job = Job().load(job["_id"], force=True)
        assert job["status"] == JobStatus.SUCCESS

        # Get the email content
        call_args = mock_send_mail.call_args
        html_content = call_args[0][1]

        # Verify timestamp format (should include date and time)
        # Looking for pattern like "2026-01-18 15:13:24 EST"
        import re

        timestamp_pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} EST"
        timestamps = re.findall(timestamp_pattern, html_content)
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

    # Mock email sending
    with mock.patch("girder.utility.mail_utils.sendMail") as mock_send_mail:
        # Submit SIVACOR job
        resp = submit_sivacor_job(server, user, fobj, stages)
        assertStatusOk(resp)
        job = resp.json

        # Verify job completion
        job = Job().load(job["_id"], force=True)
        assert job["status"] == JobStatus.SUCCESS

        # Get the email content
        call_args = mock_send_mail.call_args
        html_content = call_args[0][1]

        # Verify duration format - should contain time units
        assert (
            "second" in html_content
            or "minute" in html_content
            or "hour" in html_content
        )
