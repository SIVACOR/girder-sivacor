import ExecutionRecordsView from './views/ExecutionRecordsView';
import UsageReportView from './views/UsageReportView';
import VolumeQuotaView from './views/VolumeQuotaView';

const router = girder.router;
const events = girder.events;

router.route('sivacor/telemetry', 'sivacorTelemetry', function () {
    events.trigger('g:navigateTo', ExecutionRecordsView);
});

router.route('sivacor/usage', 'sivacorUsage', function () {
    events.trigger('g:navigateTo', UsageReportView);
});

router.route('sivacor/volumes', 'sivacorVolumes', function () {
    events.trigger('g:navigateTo', VolumeQuotaView);
});
