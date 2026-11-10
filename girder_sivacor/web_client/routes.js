import ConfigView from './views/ConfigView';
import ExecutionRecordsView from './views/ExecutionRecordsView';
import UsageReportView from './views/UsageReportView';
import VolumeQuotaView from './views/VolumeQuotaView';

const router = girder.router;
const events = girder.events;
const { exposePluginConfig } = girder.utilities.PluginUtils;

exposePluginConfig('sivacor', 'plugins/sivacor/config');

router.route('plugins/sivacor/config', 'sivacorConfig', function () {
    events.trigger('g:navigateTo', ConfigView);
});

router.route('sivacor/telemetry', 'sivacorTelemetry', function () {
    events.trigger('g:navigateTo', ExecutionRecordsView);
});

router.route('sivacor/usage', 'sivacorUsage', function () {
    events.trigger('g:navigateTo', UsageReportView);
});

router.route('sivacor/volumes', 'sivacorVolumes', function () {
    events.trigger('g:navigateTo', VolumeQuotaView);
});
