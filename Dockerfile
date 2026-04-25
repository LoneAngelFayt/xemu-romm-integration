FROM scratch

COPY root/ /
COPY --chmod=0440 root/etc/sudoers.d/broker /etc/sudoers.d/broker
